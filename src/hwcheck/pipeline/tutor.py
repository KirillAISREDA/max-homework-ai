"""Tutor (арх. §3.3): сократический диалог с уровнями подсказок 0→3.

Жёсткие правила В КОДЕ, не в промпте:
- уровень подсказки повышает FSM (одна реплика ученика без верного ответа = +1),
  LLM уровень не контролирует; верное промежуточное действие уровень не повышает;
- вопрос ребёнку задаёт код (`target_question`), модель пишет только подсказку: вопрос и
  сверка ответа говорят об одном числе (живой разбор 30.09);
- эталонное решение и ответ попадают в промпт ТОЛЬКО на уровне 3;
- resolved ставит код по детерминированной сверке ответа (compare_answers); неверный ответ после
  показанного решения закрывает разбор кодом (`closed`) — без модели;
- выход тоже проверяется: до уровня 3 реплика с числом из эталона (модель может
  решить задачу сама по условию) перегенерируется, затем заменяется заглушкой; похвала без
  засчитанного ответа — так же.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.llm.journal import llm_step
from hwcheck.pipeline.classifier import ErrorAnalysis
from hwcheck.pipeline.mathparse import (
    VARIABLE,
    action_values,
    parse_line,
    parse_value,
    school_notation,
)
from hwcheck.pipeline.reading import line_label
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import compare_answers
from hwcheck.prompts import load_prompt

MAX_HINT_LEVEL = 3
PROMPT_VERSION = "v2"  # математика: подсказка без вопроса, вопрос дописывает код
WORD_PROMPT_VERSION = "v1"  # русский: разбор орфограммы

_NUMBER_TOKEN = re.compile(r"\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+[.,]\d+|\d+")
# похвала итога; «неверно», «не верно», «верное», «правильно ли», «верно ли» — не похвала
_PRAISE = re.compile(
    r"(?<![а-яё])(?<!не )"
    r"(?:молод(?:ец|чина)|правильно|верно|отлично|умни(?:ца|чка)|так держать|здорово)"
    r"(?![а-яё])(?!\s+ли\b)",
    re.IGNORECASE,
)

SAFE_REDIRECT = "Давай не будем спешить с готовым ответом 🙂 Пересчитай этот шаг ещё раз."
SAFE_PRAISE_REDIRECT = "Пока не сходится 🙂 Посмотри на свою запись ещё раз."
SAFE_RETRY = "Хм, у меня небольшая заминка 🙂"
Violation = Literal["leak", "praise"]
# что сказать модели при перегенерации и чем заменить реплику, если и она нарушает правило
_STOP: dict[Violation, str] = {
    "leak": "СТОП: в реплике есть число из решения, а уровень подсказки ещё не 3. "
    "Переформулируй подсказку, не называя ни одного числа, которого нет в записи ученика.",
    "praise": "СТОП: ответ ученика не верный, а в реплике похвала. Переформулируй подсказку без "
    "похвалы и не говори, что задание решено.",
}
_SAFE: dict[Violation, str] = {"leak": SAFE_REDIRECT, "praise": SAFE_PRAISE_REDIRECT}


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
    # ошибочная строка ребёнка: по ней код задаёт вопрос и узнаёт верные промежуточные действия.
    # None — ошибка только в ответе или сессия из Redis до появления поля
    target_line: str | None = None
    # верные промежуточные значения, которые ребёнок уже назвал: фильтр утечек их не прячет
    known_values: list[str] = []
    # решение показано, ответ снова неверный — разбор закрыл код (не «исправил сам»)
    closed: bool = False


async def tutor_reply(
    client: LLMClient,
    session: TutorSession,
    student_message: str,
    *,
    model: str,
    prompt_version: str | None = None,
) -> tuple[str, TutorSession]:
    if session.word is not None:
        version = prompt_version or WORD_PROMPT_VERSION
        with llm_step("ru_tutor", version):
            return await _word_reply(
                client, session, session.word, student_message,
                model=model, prompt_version=version,
            )  # fmt: skip
    version = prompt_version or PROMPT_VERSION
    # compare_answers: True → решено; False и None (реплика — не ответ, «не знаю» /
    # непарсящийся текст) одинаково тратят уровень — любая реплика без верного
    # ответа считается запросом следующей подсказки, кроме верного промежуточного действия
    solved_now = _solves(student_message, session.expected or session.ref.answer)
    step = None if solved_now else _action_step(student_message, session)
    if solved_now:
        session = session.model_copy(update={"resolved": True})
    elif step is not None:
        session = session.model_copy(update={"known_values": [*session.known_values, str(step)]})
    elif session.hint_level >= MAX_HINT_LEVEL:
        return _close(session, student_message)
    else:
        session = session.model_copy(
            update={"hint_level": min(session.hint_level + 1, MAX_HINT_LEVEL)}
        )

    messages = [
        ChatMessage(role="system", content=load_prompt("tutor", version)),
        ChatMessage(role="user", content=_context(session, solved_now, step)),
        *session.history,
        ChatMessage(role="user", content=student_message),
    ]
    # перегенерация при утечке или похвале — тот же шаг и тот же промпт, отдельный вызов в журнале
    with llm_step("tutor", version):
        try:
            turn, _ = await chat_structured(client, messages, TutorTurn, model=model)
            reply = turn.reply
        except StructuredOutputError:
            reply = SAFE_RETRY  # сбой формата не должен ронять диалог с ребёнком

        if not solved_now:
            reply = await _guard(
                client,
                session,
                messages,
                reply,
                model=model,
                check_leak=session.hint_level < MAX_HINT_LEVEL,
                check_praise=step is None,
            )
            reply = f"{reply}\n{target_question(session)}"

    return reply, _remember(session, student_message, reply)


def target_question(session: TutorSession) -> str:
    """Вопрос ребёнку задаёт код: он говорит о том же числе, которым код закрывает разбор."""
    line = session.target_line
    if line is None:
        return "Какой ответ получается в задаче? Напиши его числом."
    label = line_label(line)
    if label is not None:
        return f"Сколько получается в пункте {label})? Напиши ответ числом."
    variable = VARIABLE.search(line)
    if variable is not None:
        return f"Чему равно {variable.group(1)}? Напиши ответ числом."
    return f"Сколько будет {school_notation(_left_part(line).strip())}? Напиши ответ числом."


def _close(session: TutorSession, student_message: str) -> tuple[str, TutorSession]:
    """Решение уже показано, а ответ снова неверный: код называет ответ и закрывает разбор."""
    answer = session.expected or session.ref.answer
    units = f" {session.ref.units}" if session.ref.units and not session.expected else ""
    reply = (
        f"Ничего страшного 🙂 Верный ответ: {answer}{units}. Запиши его в тетрадь."
        if answer
        else "Ничего страшного 🙂 Посмотри решение выше и запиши ответ в тетрадь."
    )
    closed = session.model_copy(update={"closed": True})
    return reply, _remember(closed, student_message, reply)


def _remember(session: TutorSession, student_message: str, reply: str) -> TutorSession:
    return session.model_copy(
        update={
            "history": [
                *session.history,
                ChatMessage(role="user", content=student_message),
                ChatMessage(role="assistant", content=reply),
            ]
        }
    )


async def _guard(
    client: LLMClient,
    session: TutorSession,
    messages: list[ChatMessage],
    reply: str,
    *,
    model: str,
    check_leak: bool,
    check_praise: bool,
) -> str:
    """Детерминированная проверка выхода: до уровня 3 реплика не должна содержать чисел эталона
    (модель способна решить задачу сама по условию), а без засчитанного ответа — хвалить.
    Одна попытка перегенерации, затем безопасная заглушка."""
    secrets = _secret_values(session) if check_leak else set()

    def violation(text: str) -> Violation | None:
        if _leaks(text, secrets):
            return "leak"  # утечка важнее: её заглушка и не хвалит
        if check_praise and _PRAISE.search(text):
            return "praise"
        return None

    first = violation(reply)
    if first is None:
        return reply
    retry_messages = [
        *messages,
        ChatMessage(role="assistant", content=reply),
        ChatMessage(role="user", content=_STOP[first]),
    ]
    try:
        turn, _ = await chat_structured(client, retry_messages, TutorTurn, model=model)
    except StructuredOutputError:
        return _SAFE[first]
    second = violation(turn.reply)
    return turn.reply if second is None else _SAFE[second]


def _action_step(message: str, session: TutorSession) -> Any | None:
    """Верное промежуточное действие ошибочной строки, которое назвал ребёнок; None — не оно."""
    if session.target_line is None:
        return None
    value = _typed_value(message)
    if value is None:
        return None
    left = _left_part(session.target_line)
    step = next((v for v in action_values(left) if _same(v, value)), None)
    if step is None or any(_same(step, parse_value(known)) for known in session.known_values):
        return None  # повтор уже названного действия — как любая неверная реплика (ревью)
    return step


def _left_part(line: str) -> str:
    return line.split("=", 1)[0]


def _same(value: Any, other: Any) -> bool:
    return other is not None and compare_answers(str(value), str(other)) is True


def _typed_value(message: str) -> Any | None:
    """«36» или пересчитанная строка «300 − 264 = 36» — число, которое назвал ребёнок."""
    value = parse_value(message)
    if value is not None:
        return value
    parsed = parse_line(message)
    return parsed.values[-1] if parsed is not None and parsed.consistent else None


def _solves(message: str, target: str) -> bool:
    """«72» или пересчитанная строка «90 - 18 = 72» — обе формы закрывают разбор."""
    value = _typed_value(message)
    return value is not None and compare_answers(str(value), target) is True


def _numeric_values(text: str) -> set[Any]:
    values = set()
    for token in _NUMBER_TOKEN.findall(text):
        value = parse_value(token)
        if value is not None:
            values.add(value)
    return values


def _secret_values(session: TutorSession) -> set[Any]:
    """Значения эталона и действий ошибочной строки минус то, что ребёнок и так видит (условие,
    его решение, уже названные им верные действия)."""
    known = _numeric_values(session.task_text)
    for step in [*session.student_steps, *session.known_values]:
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
    if session.target_line is not None:
        secrets |= set(action_values(_left_part(session.target_line)))
    return secrets - known


def _leaks(reply: str, secrets: set[Any]) -> bool:
    return bool(_numeric_values(reply) & secrets)


def _context(session: TutorSession, solved_now: bool, step: Any | None = None) -> str:
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
    if step is not None:
        parts.append(
            f"СИТУАЦИЯ: ученик верно посчитал действие: {step}. Коротко отметь это и подскажи "
            "следующий шаг, не называя его результата."
        )
    else:
        parts.append(
            "Последняя реплика ученика — не верный ответ: не хвали и не говори, что задание решено."
        )
    if session.hint_level >= 1 and session.first_error_line is not None:
        # текст строки, а не только номер: номер модель сопоставила не с той строкой (30.09)
        where = f": «{session.target_line}»" if session.target_line else ""
        parts.append(
            f"Ошибка находится в шаге {session.first_error_line}{where}. "
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


# буква как подсказка: в кавычках («д», "д", 'д') или после «буква/букву/буквой/через»
_LETTER_HINT = re.compile(
    r"«([А-Яа-яЁё])»|\"([А-Яа-яЁё])\"|'([А-Яа-яЁё])'"
    r"|(?:букв\w*|через)\s+[«\"']?([А-Яа-яЁё])(?![А-Яа-яЁё])",
    re.IGNORECASE,
)


def _secret_letters(actual: str, expected: str) -> set[str]:
    """Буквы, которые и есть ответ: различие между тем, что ребёнок написал, и верным словом."""
    written, correct = normalize_word(actual), normalize_word(expected)
    letters = {c for c, other in zip(correct, written, strict=False) if c != other}
    return letters | (set(correct) - set(written))


def _mentions_letters(message: str, letters: set[str]) -> bool:
    """Реплика называет нужную букву: «пиши через «д»» — та же подсказка ответа, что и слово."""
    if not letters:
        return False
    found = {
        match.group(match.lastindex)
        for match in _LETTER_HINT.finditer(message)
        if match.lastindex is not None
    }
    return any(normalize_word(letter) in letters for letter in found)


WORD_REDIRECT = "Не спеши 🙂 Подумай, какое правило здесь работает, и напиши слово ещё раз."


async def _word_reply(
    client: LLMClient,
    session: TutorSession,
    word: WordTutoring,
    student_message: str,
    *,
    model: str,
    prompt_version: str = "v1",
) -> tuple[str, TutorSession]:
    solved_now = _mentions(student_message, word.expected)
    if solved_now:
        session = session.model_copy(update={"resolved": True})
    else:
        session = session.model_copy(
            update={"hint_level": min(session.hint_level + 1, MAX_HINT_LEVEL)}
        )
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_tutor", prompt_version)),
        ChatMessage(role="user", content=_word_context(session, word, solved_now)),
        *session.history,
        ChatMessage(role="user", content=student_message),
    ]
    try:
        turn, _ = await chat_structured(client, messages, TutorTurn, model=model)
        reply = turn.reply
    except StructuredOutputError:
        reply = SAFE_RETRY
    # до уровня 3 ответ не должен просочиться ни словом, ни нужной буквой
    letters = _secret_letters(word.actual, word.expected)

    def leaks(text: str) -> bool:
        return _mentions(text, word.expected) or _mentions_letters(text, letters)

    if not solved_now and session.hint_level < MAX_HINT_LEVEL and leaks(reply):
        retry = [*messages, ChatMessage(role="assistant", content=reply), ChatMessage(
            role="user",
            content="СТОП: в реплике есть правильное написание слова или нужная буква, а уровень "
            "подсказки ещё не 3. Переформулируй подсказку, не называя это слово и не называя "
            "букву — только правило.",
        )]  # fmt: skip
        try:
            turn, _ = await chat_structured(client, retry, TutorTurn, model=model)
            reply = turn.reply if not leaks(turn.reply) else WORD_REDIRECT
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
        # верного написания в промпте до уровня 3 нет совсем: «НЕ называй «поздняя»» — это и
        # есть ответ, а модель его всё равно видит (ревью 17.09, I9)
        parts.append("НЕ называй верное написание слова и НЕ называй нужную букву.")
    return "\n\n".join(parts)
