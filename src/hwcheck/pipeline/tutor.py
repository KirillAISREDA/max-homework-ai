"""Tutor (арх. §3.3): сократический диалог с уровнями подсказок 0→3.

Жёсткие правила В КОДЕ, не в промпте:
- уровень подсказки повышает FSM (одна реплика ученика без верного ответа = +1),
  LLM уровень не контролирует;
- эталонное решение и ответ попадают в промпт ТОЛЬКО на уровне 3;
- resolved ставит код по детерминированной сверке ответа (compare_answers).
"""

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, chat_structured
from hwcheck.pipeline.classifier import ErrorAnalysis
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import compare_answers
from hwcheck.prompts import load_prompt

MAX_HINT_LEVEL = 3


class TutorTurn(BaseModel):
    reply: str = Field(description="Реплика тьютора ребёнку, 1-3 коротких предложения")


class TutorSession(BaseModel):
    task_text: str
    student_steps: list[str]
    student_answer: str | None
    ref: RefSolution
    error: ErrorAnalysis | None = None
    first_error_line: int | None = None
    hint_level: int = 0
    resolved: bool = False
    history: list[ChatMessage] = []


async def tutor_reply(
    client: LLMClient,
    session: TutorSession,
    student_message: str,
    *,
    model: str,
    prompt_version: str = "v1",
) -> tuple[str, TutorSession]:
    solved_now = compare_answers(student_message, session.ref.answer) is True
    if solved_now:
        session = session.model_copy(update={"resolved": True})
    else:
        session = session.model_copy(
            update={"hint_level": min(session.hint_level + 1, MAX_HINT_LEVEL)}
        )

    messages = [
        ChatMessage(role="system", content=load_prompt("tutor", prompt_version)),
        ChatMessage(role="user", content=_context(session, solved_now)),
        *session.history,
        ChatMessage(role="user", content=student_message),
    ]
    turn, _ = await chat_structured(client, messages, TutorTurn, model=model)

    session = session.model_copy(
        update={
            "history": [
                *session.history,
                ChatMessage(role="user", content=student_message),
                ChatMessage(role="assistant", content=turn.reply),
            ]
        }
    )
    return turn.reply, session


def _context(session: TutorSession, solved_now: bool) -> str:
    parts = [
        f"Задание: {session.task_text}",
        "Решение ученика:\n" + ("\n".join(session.student_steps) or "(не распознано)"),
        f"Ответ ученика: {session.student_answer or '(нет)'}",
    ]
    if solved_now:
        parts.append(
            "СИТУАЦИЯ: ученик только что дал ВЕРНЫЙ ответ. Похвали и коротко закрепи, "
            "какой навык он применил."
        )
        return "\n\n".join(parts)

    parts.append(f"Уровень подсказки: {session.hint_level} из 3.")
    if session.hint_level >= 1 and session.first_error_line is not None:
        parts.append(
            f"Ошибка находится в шаге {session.first_error_line}. "
            "Значения и правильный результат НЕ сообщай — ученик должен пересчитать сам."
        )
    if session.hint_level >= 2 and session.error is not None:
        parts.append(
            f"Тип ошибки: {session.error.error_type}, навык: {session.error.skill}. "
            f"Подскажи ПРИЁМ (как действовать), но не называй чисел из решения."
        )
    if session.hint_level >= MAX_HINT_LEVEL:
        parts.append(
            "Уровень 3 — покажи решение полностью и объясни его:\n"
            + "\n".join(session.ref.steps)
            + f"\nОтвет: {session.ref.answer}"
            + (f" {session.ref.units}" if session.ref.units else "")
        )
    return "\n\n".join(parts)
