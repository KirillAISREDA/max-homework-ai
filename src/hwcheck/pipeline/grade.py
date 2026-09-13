"""Вердикт по работе ученика: сверка с эталоном + пересчёт шагов (арх. §7).

Детерминированная логика, без LLM. Ошибочный шаг при верном ответе — «описка»
(slip), не ошибка. Непарсящийся ответ — uncertain, решает эскалация/уточнение,
а не наказание ребёнка ложной «ошибкой».
"""

import re
from typing import Literal

from pydantic import BaseModel

from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import (
    LineCheck,
    check_steps,
    compare_answers,
    last_value_matches,
)

Verdict = Literal["correct", "wrong", "uncertain"]

# пункт задания: «а)», «б)» в условии или в начале строки решения
_ITEM = re.compile(r"(?:^|[\s;:,.])([а-еa-e])\)", re.IGNORECASE)
_STEP_ITEM = re.compile(r"^\s*([а-еa-e])\)", re.IGNORECASE)
# отдельное арифметическое выражение: числа (со скобками, смешанные «8 3/7»), соединённые
# знаками; «651 + 126 306 − 138» — два выражения, «15 · 10 + (30 − 20) · 5» — одно
_NUM = r"\(*\s*\d+(?:[.,]\d+)?(?:\s+\d+\s*/\s*\d+)?\s*\)*"
_EXPRESSION = re.compile(rf"{_NUM}(?:\s*[+\-−·×*:/]\s*{_NUM})+")


class GradeResult(BaseModel):
    verdict: Verdict
    answers_match: bool | None
    first_error_line: int | None  # 1-based, первый шаг с арифметическим расхождением
    slip_lines: list[int]  # шаги с расхождением при верном итоговом ответе
    line_checks: list[LineCheck]


def grade(
    student_steps: list[str],
    student_answer: str | None,
    ref: RefSolution,
    *,
    condition: str | None = None,
) -> GradeResult:
    """`condition` — печатное условие задания: помогает перечитать знаки, спутанные OCR."""
    checks = check_steps(student_steps, condition=condition)
    if is_multipart(condition, student_steps):
        # эталон солвера — один ответ на несколько пунктов (живые логи 06.09: «80» на
        # четыре выражения); сверять с ним нечего, судим по арифметике каждой строки
        return grade_by_lines(checks)
    mismatch_lines = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    answers_match = compare_answers(student_answer, ref.answer)
    if answers_match is None and not mismatch_lines and last_value_matches(checks, ref.answer):
        # ответа нет или он фразой с несколькими числами («Отв.: на 10 яблок надо 160»),
        # но вся арифметика верна и последняя строка даёт эталон (живые логи 07.09).
        # Несовпадение не значит ошибку — строка могла быть промежуточной → uncertain
        answers_match = True

    if answers_match is None and mismatch_lines:
        # ответа нет или он не читается, но арифметика доказуемо неверна (SymPy —
        # источник истины): «803 + 169 = 753» — ошибка, а не «не уверен»
        verdict: Verdict = "wrong"
        first_error = mismatch_lines[0]
        slips: list[int] = []
    elif answers_match is None:
        verdict = "uncertain"
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


def is_multipart(condition: str | None, steps: list[str]) -> bool:
    """Два и больше пунктов «а)», «б)» (в условии или строках решения) или несколько
    отдельных примеров в условии: «№52. 651 + 126; 379 − 253; …» (живой альбом 13.09)."""
    labels = {m.group(1).lower() for m in _ITEM.finditer(condition or "")}
    step_labels = {m.group(1).lower() for s in steps if (m := _STEP_ITEM.match(s))}
    expressions = len(_EXPRESSION.findall(condition or ""))
    return len(labels) >= 2 or len(step_labels) >= 2 or expressions >= 2


def grade_by_lines(checks: list[LineCheck]) -> GradeResult:
    """Без эталонного ответа: только детерминированный пересчёт строк."""
    mismatches = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    if mismatches:
        verdict: Verdict = "wrong"
    elif any(c.doubtful for c in checks):
        verdict = "uncertain"
    elif any(c.status == "ok" for c in checks):
        verdict = "correct"
    else:
        verdict = "uncertain"
    return GradeResult(
        verdict=verdict,
        answers_match=None,
        first_error_line=mismatches[0] if mismatches else None,
        slip_lines=[],
        line_checks=checks,
    )
