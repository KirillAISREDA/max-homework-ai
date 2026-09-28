"""Объяснение к верным заданиям сводки: за что именно ребёнка похвалили.

Объяснение получают только задания, которые детерминированная проверка признала верными;
задания с ошибкой и «не уверен» в промпт не попадают вовсе — похвала не должна раскрыть ответ к
ошибочному заданию. Сбой модели сводку не роняет и не задерживает дольше таймаута: у каждого
верного задания заранее есть запасной текст из данных валидатора.
"""

import asyncio
import logging
from typing import Any

from hwcheck.bot.fsm import CheckedTask
from hwcheck.bot.summary import task_findings
from hwcheck.events import EventLog
from hwcheck.llm.base import LLMClient, StructuredOutputError
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.praise import (
    PROMPT_VERSION,
    PraiseInput,
    PraiseTask,
    accepted_praise,
    describable,
    fallback_praise,
    generate_praise,
)
from hwcheck.subjects.base import strength_of_task

logger = logging.getLogger(__name__)

# проверка и так идёт около минуты: похвала не должна заметно её удлинять
PRAISE_TIMEOUT_S = 20.0


def is_praised(index: int, item: CheckedTask) -> bool:
    return _correct_grade(index, item) is not None


def _correct_grade(index: int, item: CheckedTask) -> GradeResult | None:
    """Пересчёт задания, если оно верно и без находок; у предмета без пересчёта (языки) — None."""
    if item.grade is None or item.grade.verdict != "correct":
        return None
    if strength_of_task(task_findings(index, item)) != "ok":
        return None
    return item.grade


def with_fallback_praise(index: int, item: CheckedTask) -> CheckedTask:
    """Задание после пересчёта (ответ ученика на уточняющий вопрос): текст без вызова модели.

    Прежний текст пересчёт не переживает: он написан по другой записи.
    """
    if item.grade is None:
        return item
    grade = _correct_grade(index, item)
    praise = fallback_praise(grade) if grade is not None else None
    return item.model_copy(update={"praise": praise})


async def explain_correct(
    llm: LLMClient,
    tasks: list[CheckedTask],
    *,
    model: str,
    events: EventLog,
    user_id: int | None,
    grade: int | None = None,
) -> list[CheckedTask]:
    """Те же задания, у верных заполнен `praise`; исключений наружу не бывает."""
    try:
        return await _explained(llm, tasks, model, events, user_id, grade)
    except Exception:
        # выше — общий обработчик проверки: исключение отсюда стоило бы ребёнку всей сводки
        logger.exception("praise step failed")
        return tasks


async def _explained(
    llm: LLMClient,
    tasks: list[CheckedTask],
    model: str,
    events: EventLog,
    user_id: int | None,
    grade: int | None,
) -> list[CheckedTask]:
    grades = {i: _correct_grade(i, item) for i, item in enumerate(tasks)}
    correct = {i: result for i, result in grades.items() if result is not None}
    if not correct:
        return tasks
    texts = {i: fallback_praise(result) for i, result in correct.items()}
    told = [
        _praise_task(i, tasks[i])
        for i, result in correct.items()
        if describable(result, tasks[i].task.student_solution_steps)
    ]
    if told:
        data = PraiseInput(tasks=told, grade=grade)
        texts |= await _generated(llm, data, model, events, user_id, n_tasks=len(correct))
    return [
        item.model_copy(update={"praise": texts[i]}) if i in texts else item
        for i, item in enumerate(tasks)
    ]


async def _generated(
    llm: LLMClient,
    data: PraiseInput,
    model: str,
    events: EventLog,
    user_id: int | None,
    *,
    n_tasks: int,
) -> dict[int, str]:
    """Тексты модели, прошедшие проверку; любой сбой — пусто, остаются запасные тексты."""
    try:
        output, result = await asyncio.wait_for(
            generate_praise(llm, data, model=model), timeout=PRAISE_TIMEOUT_S
        )
        texts = accepted_praise(data, output)
    except Exception as exc:
        logger.warning("praise failed", exc_info=True)
        _log(events, "praise_failed", user_id, model=model, n_tasks=n_tasks,
             tokens=_spent(exc), error=type(exc).__name__)  # fmt: skip
        return {}
    _log(events, "praise_generated", user_id, model=model, n_tasks=n_tasks,
         n_fallback=n_tasks - len(texts), tokens=result.tokens_in + result.tokens_out)  # fmt: skip
    return texts


def _praise_task(index: int, item: CheckedTask) -> PraiseTask:
    task = item.task
    return PraiseTask(
        index=index,
        condition=task.task_text,
        steps=list(task.student_solution_steps),
        answer=task.student_answer,
    )


def _spent(exc: Exception) -> int:
    """Токены упавшего вызова: сбой формата их всё равно потратил."""
    if isinstance(exc, StructuredOutputError) and exc.result is not None:
        return exc.result.tokens_in + exc.result.tokens_out
    return 0


def _log(events: EventLog, event_type: str, user_id: int | None, **fields: Any) -> None:
    """Текст похвалы и условия в журнал не пишем — только счётчики."""
    try:
        events.log(
            event_type,
            user_id=user_id,
            component="praise",
            prompt_version=PROMPT_VERSION,
            **fields,
        )
    except Exception:
        # сбой журнала не отнимает у ребёнка ни сводку, ни уже готовый текст
        logger.exception("praise event not logged")
