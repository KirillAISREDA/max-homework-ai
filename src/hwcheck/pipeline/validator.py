"""Детерминированный Validator (арх. §3.3): пересчёт арифметики без LLM.

LLM не является источником истины: и шаги ученика, и эталон Solver'а
проверяются здесь. Расхождение Solver/Validator — эскалация, не «ошибка ребёнка».
"""

from typing import Literal

import sympy
from pydantic import BaseModel

from hwcheck.pipeline.mathparse import parse_line, parse_value

LineStatus = Literal["ok", "mismatch", "skipped"]


class LineCheck(BaseModel):
    line: str
    status: LineStatus
    values: list[str] = []  # вычисленные значения сегментов между «=» (ok и mismatch)


def check_steps(steps: list[str]) -> list[LineCheck]:
    """Строка «= (-12)/2 + (-12)/3 = -10» продолжает предыдущую (живые логи 08.09).

    Левой частью становится хвост предыдущей строки после последнего «=», но только
    если та строка сама посчиталась: хвост «b=3» из «Дано: a=5, b=3» — не выражение.
    """
    checks = []
    tail: str | None = None  # посчитанная правая часть предыдущей строки
    for line in steps:
        continues = line.lstrip().startswith("=") and tail is not None
        expression = f"{tail} {line.lstrip()}" if continues else line
        parsed = parse_line(expression)
        if parsed is None:
            checks.append(LineCheck(line=line, status="skipped"))
            tail = line if "=" not in line and parse_value(line) is not None else None
            continue
        status: LineStatus = "ok" if parsed.consistent else "mismatch"
        checks.append(LineCheck(line=line, status=status, values=[str(v) for v in parsed.values]))
        tail = expression.rsplit("=", 1)[-1].strip()
    return checks


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
