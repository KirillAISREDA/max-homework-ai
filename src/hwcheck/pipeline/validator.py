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
    values: list[str] = []  # вычисленные значения сегментов между «=» (для mismatch)


def check_steps(steps: list[str]) -> list[LineCheck]:
    checks = []
    for line in steps:
        parsed = parse_line(line)
        if parsed is None:
            checks.append(LineCheck(line=line, status="skipped"))
        elif parsed.consistent:
            checks.append(LineCheck(line=line, status="ok"))
        else:
            checks.append(
                LineCheck(line=line, status="mismatch", values=[str(v) for v in parsed.values])
            )
    return checks


def compare_answers(student_answer: str | None, ref_answer: str | None) -> bool | None:
    """True/False — ответы сравнимы; None — хотя бы один не парсится (→ uncertain)."""
    if not student_answer or not ref_answer:
        return None
    student = parse_value(student_answer)
    ref = parse_value(ref_answer)
    if student is None or ref is None:
        return None
    return bool(sympy.simplify(student - ref) == 0)
