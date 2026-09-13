"""Детерминированный Validator (арх. §3.3): пересчёт арифметики без LLM.

LLM не является источником истины: и шаги ученика, и эталон Solver'а
проверяются здесь. Расхождение Solver/Validator — эскалация, не «ошибка ребёнка».
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

import sympy
from pydantic import BaseModel

from hwcheck.pipeline.mathparse import (
    EquationLine,
    ParsedLine,
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
    # уравнение («3x + 4 = 19»): values — корень ([верный, записанный] при mismatch);
    # присваивание «S = 6 * 4» и ответ «x = 7» — посчитанная работа, не уравнение
    equation: bool = False
    # корень сменился, но это может быть новое уравнение, а не ошибка → «не уверен»
    doubtful: bool = False


@dataclass
class _Chain:
    """Открытая цепочка преобразований одного уравнения."""

    variable: str
    root: Any
    origin: Literal["equation", "assignment"]  # чем цепочка началась
    numbers: frozenset[str]  # числа последней строки цепочки


# столько общих чисел с предыдущей строкой — и новая строка точно её преобразует
TRANSFORMATION_SHARED_NUMBERS = 2

# строки столбика: число; знак с числом («+169», «− 358»); знак отдельно; черта
_COLUMN_NUMBER = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
_COLUMN_SIGNED = re.compile(r"^\s*([+\-−×*·])\s*(\d+(?:[.,]\d+)?)\s*$")
_COLUMN_SIGN = re.compile(r"^\s*([+\-−×*·])\s*$")
_COLUMN_RULE = re.compile(r"^\s*[-_—–=]{2,}\s*$")
_MULTIPLY = "×*·"
# бинарный оператор между числами/скобками: «Вычисли: 15» — не оператор, «(-12)» — знак
_OPERATOR = re.compile(r"(?<=[\d)])\s*([+\-−*·×:/])\s*(?=[\d(])")


def check_steps(steps: list[str], *, condition: str | None = None) -> list[LineCheck]:
    """Строка «= (-12)/2 + (-12)/3 = -10» продолжает предыдущую (живые логи 08.09).

    Левой частью становится хвост предыдущей строки после последнего «=», но только
    если та строка сама посчиталась: хвост «b=3» из «Дано: a=5, b=3» — не выражение.
    `condition` — печатное условие: по нему «:» на месте «·» перечитывается (`_reread`).
    """
    checks = []
    tail: str | None = None  # посчитанная правая часть предыдущей строки
    chain: _Chain | None = None  # открытая цепочка преобразований уравнения
    columns = _column_results(steps)
    for index, line in enumerate(steps):
        continues = line.lstrip().startswith("=") and tail is not None
        expression = f"{tail} {line.lstrip()}" if continues else line
        expression = columns.get(index, expression)
        parsed = parse_line(expression)
        equation = parse_equation(line) if parsed is None else None
        if equation is not None:
            check, chain = _check_equation(line, equation, chain)
            checks.append(check)
            tail = None
            continue
        if parsed is None:
            checks.append(LineCheck(line=line, status="skipped"))
            tail = line if "=" not in line and parse_value(line) is not None else None
            continue
        if not parsed.consistent and condition:
            reread = _reread(expression, condition)
            parsed = reread if reread is not None and reread.consistent else parsed
        status: LineStatus = "ok" if parsed.consistent else "mismatch"
        checks.append(LineCheck(line=line, status=status, values=[str(v) for v in parsed.values]))
        tail = expression.rsplit("=", 1)[-1].strip()
    return checks


def _reread(expression: str, condition: str) -> ParsedLine | None:
    """«:» ученика там, где в печатном условии «·», — умножение (OCR путает знаки).

    Живые логи 06.09, 08.09: учебник «15 · 10 + (30 − 20) · 5», транскрипция тетради
    «15 * 10 + (30 - 20) : 5». Операторы первого сегмента сверяются с операторами
    условия по позициям, поэтому только строка, переписывающая условие целиком;
    «:» на месте печатного «:» не трогается. Настоящая ошибка не маскируется: строка
    станет верной, только если записанный результат совпал с умножением.
    """
    first, rest = expression.split("=", 1)
    line_ops = list(_OPERATOR.finditer(first))
    printed = [m.group(1) for m in _OPERATOR.finditer(condition.split("=", 1)[0])]
    if len(line_ops) != len(printed):
        return None
    swaps = [
        m for m, op in zip(line_ops, printed, strict=True) if m.group(1) == ":" and op in _MULTIPLY
    ]
    if not swaps:
        return None
    rebuilt = first
    for match in reversed(swaps):
        start, end = match.span(1)
        rebuilt = rebuilt[:start] + "*" + rebuilt[end:]
    return parse_line(f"{rebuilt}={rest}")


def _column_results(steps: list[str]) -> dict[int, str]:
    """Столбик → равенство на строке результата: {индекс результата: «803 + 169 = 972»}.

    Формы записи: «803 / +169 / 972», «+803 / 169 / 972», «1000 / − / 358 / 642»,
    черта между операндами и результатом необязательна. Умножение — только на
    однозначное число: у многозначного множителя под чертой идут промежуточные
    произведения, и первое из них нельзя принять за результат. Всё прочее не трогаем.
    """
    kinds = [_column_kind(line) for line in steps]
    results: dict[int, str] = {}
    i = 0
    while i < len(steps):
        found = _match_column(steps, kinds, i)
        if found is None:
            i += 1
            continue
        result_index, equality = found
        results[result_index] = equality
        i = result_index + 1
    return results


def _column_kind(line: str) -> str:
    if _COLUMN_NUMBER.match(line):
        return "N"
    if _COLUMN_SIGNED.match(line):
        return "S"
    if _COLUMN_SIGN.match(line):
        return "O"
    if _COLUMN_RULE.match(line):
        return "R"
    return "?"


def _match_column(steps: list[str], kinds: list[str], start: int) -> tuple[int, str] | None:
    for pattern in ("NS", "SN", "NON"):
        end = start + len(pattern)
        if "".join(kinds[start:end]) != pattern:
            continue
        result = end + 1 if end < len(kinds) and kinds[end] == "R" else end
        if result >= len(kinds) or kinds[result] != "N":
            continue
        lines = steps[start:end]
        if pattern == "NS":
            left, (op, right) = _number(lines[0]), _signed(lines[1])
        elif pattern == "SN":
            (op, left), right = _signed(lines[0]), _number(lines[1])
        else:
            left, op, right = _number(lines[0]), lines[1].strip(), _number(lines[2])
        if op in _MULTIPLY and min(len(left), len(right)) > 1:
            return None
        if result == end and min(_digits(left), _digits(right)) < 2:
            # без черты «5 / -3 / 3» — скорее три отдельных ответа, чем столбик (ревью):
            # столбиком считают многозначные числа, однозначные — только под чертой
            continue
        return result, f"{left} {op} {right} = {_number(steps[result])}"
    return None


def _digits(number: str) -> int:
    return sum(ch.isdigit() for ch in number)


def _number(line: str) -> str:
    match = _COLUMN_NUMBER.match(line)
    assert match is not None
    return match.group(1)


def _signed(line: str) -> tuple[str, str]:
    match = _COLUMN_SIGNED.match(line)
    assert match is not None
    return match.group(1), match.group(2)


def _check_equation(
    line: str, equation: EquationLine, chain: _Chain | None
) -> tuple[LineCheck, _Chain | None]:
    """Преобразование уравнения сохраняет корень (живые логи 07.09, №462).

    Корень считается по каждой паре соседних сегментов с переменной; числовые пары
    («x = 12 - 5 = 8») проверяются как обычная арифметика. Смена корня относительно
    открытой цепочки той же переменной — ошибка, только когда строка точно продолжает
    цепочку: выделение переменной («x = 12 + 5», «x = 17») после уравнения, либо
    уравнение, преобразующее предыдущую строку (общие числа). Уравнение без связи с
    предыдущим («2x + 4 = 18», затем «x - 3 = 5») может быть новым заданием — это
    «не уверен», а не ложная ошибка (ревью). Присваивания подряд («S = 6 * 4»,
    «S = 5 * 5») — разные вычисления. «x = число» и маркер пункта закрывают цепочку.
    Нет единственного корня (два корня, тождество, степень > 2) — строка не проверяется.
    """
    if equation.item_marker:
        chain = None
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
    if chain is not None and chain.variable != equation.variable:
        chain = None
    wrong_step, doubtful = _root_change(equation, root, chain)

    # при ошибке шага values[0] — верный корень цепочки (цель разбора для тьютора)
    values = [str(chain.root), str(root)] if wrong_step and chain is not None else [str(root)]
    status: LineStatus = "mismatch" if wrong_step or not consistent else "ok"
    check = LineCheck(
        line=line,
        status="skipped" if doubtful and status == "ok" else status,
        values=values,
        equation=equation.kind == "equation",
        doubtful=doubtful,
    )
    if equation.kind == "answer":
        return check, None
    continues = chain is not None and chain.origin == "equation" and not doubtful
    origin: Literal["equation", "assignment"] = (
        "equation" if equation.kind == "equation" or continues else "assignment"
    )
    return check, _Chain(equation.variable, root, origin, equation.numbers)


def _root_change(equation: EquationLine, root: Any, chain: _Chain | None) -> tuple[bool, bool]:
    """(ошибка шага, сомнение), когда корень строки отличается от корня цепочки."""
    if chain is None or sympy.simplify(root - chain.root) == 0:
        return False, False
    after_equation = chain.origin == "equation"
    if equation.kind == "equation":
        shared = len(equation.numbers & chain.numbers)
        transforms = after_equation and shared >= TRANSFORMATION_SHARED_NUMBERS
        return transforms, not transforms
    # «x = 17» или «x = 12 + 5» после уравнения — выделение переменной из него
    return equation.kind == "answer" or after_equation, False


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
