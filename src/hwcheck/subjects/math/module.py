"""Математика через контракт `SubjectModule` — обёртка над bot/check.py и pipeline/* без изменения
поведения: распознавание двухэтапным vision, эталон солвера, пересчёт SymPy, тьютор.

`GradeResult` остаётся источником вердикта; в `Finding` он переводится так, чтобы строки сводки
ученику не изменились (тесты test_bot.py).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hwcheck.bot.check import CheckModels, check_task, recognize_photo
from hwcheck.pipeline.classifier import classify_error
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import FileCache, RefSolution, StructuredOutputError
from hwcheck.pipeline.tutor import TutorSession
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import (
    Finding,
    KnowledgeBase,
    Reference,
    SubjectPage,
    SubjectTask,
    TaskResult,
    Trust,
    Usage,
)

logger = logging.getLogger(__name__)

# «не уверен» без вопроса — с причиной, а не безличное «покажи взрослому» (из handlers.py)
UNCERTAIN_TEXT = {
    "unreadable": "часть записи неразборчива",
    "ambiguous_equation": "не уверен в ходе решения уравнений",
    "answer_unparseable": "не разобрал ответ",
    "no_answer": "не нашёл итоговый ответ",
    "steps_unparseable": "не смог разобрать решение",
    "column_unreadable": "не смог прочитать деление уголком",
}
_DIGITS = re.compile(r"\d+")


def to_subject_task(task: VisionTask) -> SubjectTask:
    return SubjectTask(
        number=str(task.number),
        number_on_page=task.number_on_page,
        condition=task.task_text,
        lines=list(task.student_solution_steps),
        answer=task.student_answer,
        confidence=task.confidence,
    )


def to_vision_task(task: SubjectTask) -> VisionTask:
    match = _DIGITS.search(task.number)
    return VisionTask(
        number=int(match.group()) if match else 0,
        task_text=task.condition,
        student_solution_steps=list(task.lines),
        student_answer=task.answer,
        confidence=task.confidence,
        number_on_page=task.number_on_page,
    )


def findings_from_grade(task_index: int, grade: GradeResult) -> list[Finding]:
    """`wrong` → verified ошибка в первой расходящейся строке; `uncertain` → candidate."""
    if grade.verdict == "wrong":
        line = grade.first_error_line
        expected = None
        if line is not None:
            check = grade.line_checks[line - 1]
            expected = check.values[0] if check.status == "mismatch" and check.values else None
        return [
            Finding(
                task_index=task_index,
                kind="arithmetic",
                strength="verified",
                line=line,
                expected=expected,
            )  # fmt: skip
        ]
    if grade.verdict == "uncertain":
        detail = UNCERTAIN_TEXT.get(grade.uncertain_reason or "", "не уверен в проверке")
        return [
            Finding(
                task_index=task_index,
                kind="uncertain",
                strength="candidate",
                detail=detail,
                rule_code=None,
            )  # fmt: skip
        ]
    return []


class MathModule:
    code = "math"

    def __init__(
        self, llm: VisionAndChatClient, models: CheckModels, cache: FileCache | None
    ) -> None:
        self._llm = llm
        self._models = models
        self._cache = cache

    async def recognize(self, image: bytes) -> SubjectPage:
        recognized = await recognize_photo(self._llm, image, self._models)
        page, rec = recognized.page, recognized.rec
        role = recognized.role if recognized.role in ("textbook", "notebook") else "unknown"
        return SubjectPage(
            subject=self.code,
            role=role,
            tasks=[to_subject_task(t) for t in (page.tasks if page else [])],
            comment=page.page_comment if page else None,
            transcript=rec.raw,
            usage=Usage(calls=rec.attempts + 1, tokens=rec.tokens_in + rec.tokens_out),
        )

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]:
        """Эталон солвера считается внутри `check_task` (кэш, самопроверка) — здесь только
        отмечаем, у каких заданий есть условие; сам эталон появится в `TaskResult.reference`."""
        return [
            Reference(task_number=t.number, origin="derived", trust="unverified", payload={})
            for t in tasks
            if t.condition.strip()
        ]

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]:
        results: list[TaskResult] = []
        for index, task in enumerate(tasks):
            checked = await check_task(self._llm, to_vision_task(task), self._models, self._cache)
            reference = None
            if checked.ref is not None:
                trust: Trust = "verified" if checked.ref_status == "ok" else "unverified"
                reference = Reference(
                    task_number=task.number,
                    origin="derived",
                    trust=trust,
                    payload={"ref": checked.ref.model_dump()},
                )
            payload: dict[str, Any] = {
                "grade": checked.grade.model_dump(),
                "ref_status": checked.ref_status,
                "solver_from_cache": checked.solved.from_cache if checked.solved else None,
                "solver_tokens": (
                    checked.solver_result.tokens_in + checked.solver_result.tokens_out
                    if checked.solver_result
                    else 0
                ),
            }
            results.append(
                TaskResult(
                    task_index=index,
                    findings=findings_from_grade(index, checked.grade),
                    reference=reference,
                    payload=payload,
                )
            )
        return results

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession:
        grade = GradeResult.model_validate(result.payload["grade"])
        ref = (
            RefSolution.model_validate(result.reference.payload["ref"])
            if result.reference is not None and "ref" in result.reference.payload
            else _pseudo_ref(grade)
        )
        error = None
        if result.reference is not None and "ref" in result.reference.payload:
            try:
                error = await classify_error(
                    self._llm, task.condition, task.lines, task.answer, ref, grade,
                    model=self._models.structure,
                )  # fmt: skip
            except StructuredOutputError:
                logger.warning("classifier failed")
        return TutorSession(
            task_text=task.condition or "\n".join(task.lines),
            student_steps=task.lines,
            student_answer=task.answer,
            ref=ref,
            error=error,
            first_error_line=grade.first_error_line,
            expected=_error_line_value(grade),
        )


def _error_line_value(result: GradeResult) -> str | None:
    if result.first_error_line is None:
        return None
    check = result.line_checks[result.first_error_line - 1]
    return check.values[0] if check.status == "mismatch" and check.values else None


def _pseudo_ref(result: GradeResult) -> RefSolution:
    """Для задания без условия: «эталон» — верное значение первой ошибочной строки."""
    for check in result.line_checks:
        if check.status == "mismatch" and check.values:
            return RefSolution(steps=[], answer=check.values[0], units=None)
    return RefSolution(steps=[], answer="", units=None)
