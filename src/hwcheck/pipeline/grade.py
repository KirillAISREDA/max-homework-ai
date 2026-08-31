"""Вердикт по работе ученика: сверка с эталоном + пересчёт шагов (арх. §7).

Детерминированная логика, без LLM. Ошибочный шаг при верном ответе — «описка»
(slip), не ошибка. Непарсящийся ответ — uncertain, решает эскалация/уточнение,
а не наказание ребёнка ложной «ошибкой».
"""

from typing import Literal

from pydantic import BaseModel

from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import LineCheck, check_steps, compare_answers

Verdict = Literal["correct", "wrong", "uncertain"]


class GradeResult(BaseModel):
    verdict: Verdict
    answers_match: bool | None
    first_error_line: int | None  # 1-based, первый шаг с арифметическим расхождением
    slip_lines: list[int]  # шаги с расхождением при верном итоговом ответе
    line_checks: list[LineCheck]


def grade(student_steps: list[str], student_answer: str | None, ref: RefSolution) -> GradeResult:
    checks = check_steps(student_steps)
    mismatch_lines = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    answers_match = compare_answers(student_answer, ref.answer)

    if answers_match is None:
        verdict: Verdict = "uncertain"
        first_error = None
        slips = []
    elif answers_match:
        verdict = "correct"
        first_error = None
        slips = mismatch_lines
    else:
        verdict = "wrong"
        first_error = mismatch_lines[0] if mismatch_lines else None
        slips = []

    return GradeResult(
        verdict=verdict,
        answers_match=answers_match,
        first_error_line=first_error,
        slip_lines=slips,
        line_checks=checks,
    )
