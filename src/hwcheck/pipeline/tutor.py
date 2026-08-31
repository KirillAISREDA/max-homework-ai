"""Tutor (арх. §3.3): сократический диалог с уровнями подсказок 0→3.

Жёсткие правила В КОДЕ, не в промпте:
- уровень подсказки повышает FSM (одна реплика ученика без верного ответа = +1),
  LLM уровень не контролирует;
- эталонное решение и ответ попадают в промпт ТОЛЬКО на уровне 3;
- resolved ставит код по детерминированной сверке ответа (compare_answers);
- выход тоже проверяется: до уровня 3 реплика с числом из эталона (модель может
  решить задачу сама по условию) перегенерируется, затем заменяется заглушкой.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.pipeline.classifier import ErrorAnalysis
from hwcheck.pipeline.mathparse import parse_line, parse_value
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import compare_answers
from hwcheck.prompts import load_prompt

MAX_HINT_LEVEL = 3

_NUMBER_TOKEN = re.compile(r"\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+[.,]\d+|\d+")

SAFE_REDIRECT = (
    "Давай не будем спешить с готовым ответом 🙂 "
    "Пересчитай этот шаг ещё раз и напиши, что у тебя получается."
)
SAFE_RETRY = "Хм, у меня небольшая заминка. Напиши ещё раз, что получается в этом шаге?"


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
    # history никогда не мутируется на месте — только пересборка списком:
    # model_copy(update=...) делает shallow-копию, append сломал бы другие копии сессии
    history: list[ChatMessage] = []


async def tutor_reply(
    client: LLMClient,
    session: TutorSession,
    student_message: str,
    *,
    model: str,
    prompt_version: str = "v1",
) -> tuple[str, TutorSession]:
    # compare_answers: True → решено; False и None (реплика — не ответ, «не знаю» /
    # непарсящийся текст) одинаково тратят уровень — любая реплика без верного
    # ответа считается запросом следующей подсказки
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
    try:
        turn, _ = await chat_structured(client, messages, TutorTurn, model=model)
        reply = turn.reply
    except StructuredOutputError:
        reply = SAFE_RETRY  # сбой формата не должен ронять диалог с ребёнком

    if not solved_now and session.hint_level < MAX_HINT_LEVEL:
        reply = await _guard_leak(client, session, messages, reply, model=model)

    session = session.model_copy(
        update={
            "history": [
                *session.history,
                ChatMessage(role="user", content=student_message),
                ChatMessage(role="assistant", content=reply),
            ]
        }
    )
    return reply, session


async def _guard_leak(
    client: LLMClient,
    session: TutorSession,
    messages: list[ChatMessage],
    reply: str,
    *,
    model: str,
) -> str:
    """Детерминированная проверка выхода: до уровня 3 реплика не должна содержать
    чисел эталона (модель способна решить задачу сама по условию). Одна попытка
    перегенерации, затем безопасная заглушка."""
    secrets = _secret_values(session)
    if not _leaks(reply, secrets):
        return reply
    retry_messages = [
        *messages,
        ChatMessage(role="assistant", content=reply),
        ChatMessage(
            role="user",
            content=(
                "СТОП: в реплике есть число из решения, а уровень подсказки ещё не 3. "
                "Переформулируй подсказку, не называя ни одного числа, "
                "которого нет в записи ученика."
            ),
        ),
    ]
    try:
        turn, _ = await chat_structured(client, retry_messages, TutorTurn, model=model)
    except StructuredOutputError:
        return SAFE_REDIRECT
    return turn.reply if not _leaks(turn.reply, secrets) else SAFE_REDIRECT


def _numeric_values(text: str) -> set[Any]:
    values = set()
    for token in _NUMBER_TOKEN.findall(text):
        value = parse_value(token)
        if value is not None:
            values.add(value)
    return values


def _secret_values(session: TutorSession) -> set[Any]:
    """Значения эталона минус то, что ребёнок и так видит (условие, его решение)."""
    known = _numeric_values(session.task_text)
    for step in session.student_steps:
        known |= _numeric_values(step)
    if session.student_answer:
        known |= _numeric_values(session.student_answer)

    secrets = set()
    answer = parse_value(session.ref.answer)
    if answer is not None:
        secrets.add(answer)
    for step in session.ref.steps:
        parsed = parse_line(step)
        if parsed is not None:
            secrets.add(parsed.values[-1])
    return secrets - known


def _leaks(reply: str, secrets: set[Any]) -> bool:
    return bool(_numeric_values(reply) & secrets)


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
