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


class WordTutoring(BaseModel):
    """Разбор орфограммы (русский, спецификация §6): уровни 0 «какая орфограмма?», 1 правило,
    2 пример, 3 написание."""

    actual: str
    expected: str
    sentence: str
    rule_code: str | None = None
    rule_title: str | None = None
    rule_statement: str | None = None
    rule_example: str | None = None


class TutorSession(BaseModel):
    task_text: str
    student_steps: list[str]
    student_answer: str | None
    ref: RefSolution
    error: ErrorAnalysis | None = None
    first_error_line: int | None = None
    # верное значение ошибочной строки (SymPy): разбор закрывается им, а не общим ответом
    # эталона — у задания из нескольких пунктов эталон один на всё (живые логи 06.09)
    expected: str | None = None
    hint_level: int = 0
    resolved: bool = False
    # history никогда не мутируется на месте — только пересборка списком:
    # model_copy(update=...) делает shallow-копию, append сломал бы другие копии сессии
    history: list[ChatMessage] = []
    # русский: разбор орфограммы вместо арифметики — None означает прежнее (математическое)
    # поведение; поле последним, чтобы старые состояния в Redis валидировались без миграции
    word: WordTutoring | None = None


async def tutor_reply(
    client: LLMClient,
    session: TutorSession,
    student_message: str,
    *,
    model: str,
    prompt_version: str = "v1",
) -> tuple[str, TutorSession]:
    if session.word is not None:
        return await _word_reply(client, session, session.word, student_message, model=model)
    # compare_answers: True → решено; False и None (реплика — не ответ, «не знаю» /
    # непарсящийся текст) одинаково тратят уровень — любая реплика без верного
    # ответа считается запросом следующей подсказки
    solved_now = _solves(student_message, session.expected or session.ref.answer)
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


def _solves(message: str, target: str) -> bool:
    """«72» или пересчитанная строка «90 - 18 = 72» — обе формы закрывают разбор."""
    if compare_answers(message, target) is True:
        return True
    parsed = parse_line(message)
    return (
        parsed is not None
        and parsed.consistent
        and compare_answers(str(parsed.values[-1]), target) is True
    )


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
    for value in (session.ref.answer, session.expected):
        parsed_value = parse_value(value) if value else None
        if parsed_value is not None:
            secrets.add(parsed_value)
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
        if session.expected is not None and session.first_error_line is not None:
            parts.append(
                f"Верный результат шага {session.first_error_line}: {session.expected}. "
                "Разбирай именно этот шаг."
            )
    return "\n\n".join(parts)


def normalize_word(word: str) -> str:
    return word.lower().replace("ё", "е").strip("-")


def _mentions(message: str, word: str) -> bool:
    return normalize_word(word) in {normalize_word(t) for t in re.findall(r"[А-Яа-яЁё-]+", message)}


WORD_REDIRECT = "Не спеши 🙂 Подумай, какое правило здесь работает, и напиши слово ещё раз."


async def _word_reply(
    client: LLMClient,
    session: TutorSession,
    word: WordTutoring,
    student_message: str,
    *,
    model: str,
) -> tuple[str, TutorSession]:
    solved_now = _mentions(student_message, word.expected)
    if solved_now:
        session = session.model_copy(update={"resolved": True})
    else:
        session = session.model_copy(
            update={"hint_level": min(session.hint_level + 1, MAX_HINT_LEVEL)}
        )
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_tutor", "v1")),
        ChatMessage(role="user", content=_word_context(session, word, solved_now)),
        *session.history,
        ChatMessage(role="user", content=student_message),
    ]
    try:
        turn, _ = await chat_structured(client, messages, TutorTurn, model=model)
        reply = turn.reply
    except StructuredOutputError:
        reply = SAFE_RETRY
    if not solved_now and session.hint_level < MAX_HINT_LEVEL and _mentions(reply, word.expected):
        retry = [*messages, ChatMessage(role="assistant", content=reply), ChatMessage(
            role="user",
            content="СТОП: в реплике есть правильное написание слова, а уровень подсказки ещё "
            "не 3. Переформулируй подсказку, не называя это слово и не называя букву.",
        )]  # fmt: skip
        try:
            turn, _ = await chat_structured(client, retry, TutorTurn, model=model)
            reply = turn.reply if not _mentions(turn.reply, word.expected) else WORD_REDIRECT
        except StructuredOutputError:
            reply = WORD_REDIRECT
    history = [*session.history, ChatMessage(role="user", content=student_message),
               ChatMessage(role="assistant", content=reply)]  # fmt: skip
    return reply, session.model_copy(update={"history": history})


def _word_context(session: TutorSession, word: WordTutoring, solved_now: bool) -> str:
    parts = [
        f"Предложение из тетради: {word.sentence}",
        f"Слово, как написал ученик: {word.actual}",
    ]
    if solved_now:
        parts.append("СИТУАЦИЯ: ученик написал слово ВЕРНО. Похвали и коротко назови правило.")
        return "\n\n".join(parts)
    parts.append(f"Уровень подсказки: {session.hint_level} из 3.")
    if word.rule_title:
        parts.append(f"Орфограмма: {word.rule_title}.")
    if session.hint_level >= 1 and word.rule_statement:
        parts.append(f"Правило (можно пересказать ребёнку): {word.rule_statement}")
    if session.hint_level >= 2 and word.rule_example:
        parts.append(f"Пример на это правило с ДРУГИМ словом: {word.rule_example}")
    if session.hint_level >= MAX_HINT_LEVEL:
        parts.append(f"Уровень 3 — назови верное написание «{word.expected}» и объясни его.")
    else:
        parts.append(f"НЕ называй верное написание («{word.expected}») и НЕ называй нужную букву.")
    return "\n\n".join(parts)
