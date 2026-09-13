"""Детерминированный Validator (арх. §3.3): пересчёт арифметики без LLM.

LLM не является источником истины: и шаги ученика, и эталон Solver'а
проверяются здесь. Расхождение Solver/Validator — эскалация, не «ошибка ребёнка».
"""

from typing import Any, Literal

import sympy
from pydantic import BaseModel

from hwcheck.pipeline.mathparse import (
    EquationLine,
    parse_equation,
    parse_line,
    parse_value,
    solve_single_root,
)

LineStatus = Literal["ok", "mismatch", "skipped"]


class LineCheck(BaseModel):
    line: str
    status: LineStatus
    values: list[str] = []  # вычисленные значения сегментов между «=» (ok и mismatch)
    # строка уравнения: values — корень ([верный, записанный] при mismatch)
    equation: bool = False


_Root = tuple[str, Any]  # (буква переменной, корень) последней строки цепочки уравнения


def check_steps(steps: list[str]) -> list[LineCheck]:
    """Строка «= (-12)/2 + (-12)/3 = -10» продолжает предыдущую (живые логи 08.09).

    Левой частью становится хвост предыдущей строки после последнего «=», но только
    если та строка сама посчиталась: хвост «b=3» из «Дано: a=5, b=3» — не выражение.
    """
    checks = []
    tail: str | None = None  # посчитанная правая часть предыдущей строки
    root: _Root | None = None  # корень предыдущей строки уравнения в текущей цепочке
    for line in steps:
        continues = line.lstrip().startswith("=") and tail is not None
        expression = f"{tail} {line.lstrip()}" if continues else line
        parsed = parse_line(expression)
        equation = parse_equation(line) if parsed is None else None
        if equation is not None:
            check, root = _check_equation(line, equation, root)
            checks.append(check)
            tail = None
            continue
        if parsed is None:
            checks.append(LineCheck(line=line, status="skipped"))
            tail = line if "=" not in line and parse_value(line) is not None else None
            continue
        status: LineStatus = "ok" if parsed.consistent else "mismatch"
        checks.append(LineCheck(line=line, status=status, values=[str(v) for v in parsed.values]))
        tail = expression.rsplit("=", 1)[-1].strip()
    return checks


def _check_equation(
    line: str, equation: EquationLine, previous: _Root | None
) -> tuple[LineCheck, _Root | None]:
    """Преобразование уравнения сохраняет корень (живые логи 07.09, №462).

    Корень считается по каждой паре соседних сегментов с переменной; числовые пары
    («x = 12 - 5 = 8») проверяются как обычная арифметика. Корень поменялся по сравнению
    с предыдущей строкой той же переменной — ошибка на этой строке; дальше цепочка
    сверяется с новым корнем, чтобы не размножать одну ошибку. Строка «x = число»
    закрывает цепочку: следующее уравнение того же задания начинается заново.
    Нет единственного корня (два корня, тождество, степень > 2) — строка не проверяется.
    """
    segments = equation.segments
    roots = []
    numeric_ok = True
    for left, right in zip(segments, segments[1:], strict=False):
        if left.free_symbols or right.free_symbols:
            found = solve_single_root(left, right)
            if found is None:
                return LineCheck(line=line, status="skipped"), None
            roots.append(found)
        elif sympy.simplify(left - right) != 0:
            numeric_ok = False
    root = roots[0]
    consistent = numeric_ok and all(sympy.simplify(r - root) == 0 for r in roots[1:])
    expected = previous[1] if previous and previous[0] == equation.variable else None
    if expected is not None and sympy.simplify(root - expected) != 0:
        consistent = False
    values = [str(expected if expected is not None else root)]
    if not consistent:
        values.append(str(root))
    check = LineCheck(
        line=line, status="ok" if consistent else "mismatch", values=values, equation=True
    )
    next_root = None if equation.solved_form else (equation.variable, root)
    return check, next_root


def last_value_matches(checks: list[LineCheck], ref_answer: str | None) -> bool:
    """Последняя строка с «=» посчиталась, и её значение совпадает с эталонным ответом.

    Не прочиталась итоговая строка — промежуточная, совпавшая с эталоном, не в счёт.
    """
    equalities = [c for c in checks if "=" in c.line]
    if not equalities or equalities[-1].status != "ok":
        return False
    return compare_answers(equalities[-1].values[-1], ref_answer) is True


def compare_answers(student_answer: str | None, ref_answer: str | None) -> bool | None:
    """True/False — ответы сравнимы; None — хотя бы один не парсится (→ uncertain)."""
    if not student_answer or not ref_answer:
        return None
    student = parse_value(student_answer)
    ref = parse_value(ref_answer)
    if student is None or ref is None:
        return None
    return bool(sympy.simplify(student - ref) == 0)
