"""Похвала за верное решение: какой приём ученик применил правильно и почему решение верное.

Жёсткие правила В КОДЕ, не в промпте:
- верность решения устанавливает валидатор; сюда приходят только уже верные задания, модель
  ничего не оценивает — она формулирует;
- один вызов на домашку (тариф GigaChat — один одновременный запрос);
- выход проверяется детерминированно: числа только из записи этого задания, без слов об
  ошибках, не длиннее пары предложений. Что не прошло — заменяется текстом из данных валидатора.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, LLMResult, chat_structured
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.mathparse import parse_value
from hwcheck.prompts import load_prompt

PROMPT_VERSION = "v1"
MAX_PRAISE_CHARS = 220
# в промпт — начало записи: длинное условие и хвост решения приём не меняют, а вызов удлиняют
MAX_CONDITION_CHARS = 500
MAX_STEPS = 12

# смешанное число, дробь, десятичная дробь, целое — как читает запись валидатор
_NUMBER = re.compile(r"\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+[.,]\d+|\d+")
# части записи «7 1/8»: ребёнку можно назвать и дробь «1/8», и числитель «1»
_NUMBER_PART = re.compile(r"\d+\s*/\s*\d+|\d+[.,]\d+|\d+")
_DIGITS = re.compile(r"\d+")
# похвала за верное решение не говорит об ошибках; «но» — только отдельным словом:
# на «но» кончаются «правильно» и «верно»
_ERROR_MARKER = re.compile(
    r"ошиб|неверн|неправильн|исправ|однако|(?<![а-яё])(?:но|зато|хотя)(?![а-яё])",
    re.IGNORECASE,
)
_SPACES = re.compile(r"\s+")

FALLBACK_ANSWER = "Ответ сошёлся с моим пересчётом."


class PraiseTask(BaseModel):
    index: int = Field(description="Номер задания в домашке — по нему текст вернётся к заданию")
    condition: str = ""
    steps: list[str] = Field(default_factory=list, description="Шаги ученика, по строкам")
    answer: str | None = None


class PraiseInput(BaseModel):
    tasks: list[PraiseTask]
    grade: int | None = Field(default=None, description="Класс ученика, если известен")


class PraiseItem(BaseModel):
    index: int
    technique: str = Field(description="Какой приём применён, 3–8 слов")
    why: str = Field(description="Почему решение верное, одно предложение на «ты»")


class PraiseOutput(BaseModel):
    items: list[PraiseItem]


async def generate_praise(
    client: LLMClient,
    data: PraiseInput,
    *,
    model: str,
    prompt_version: str = PROMPT_VERSION,
) -> tuple[PraiseOutput, LLMResult]:
    """Единственный вызов модели шага; сбой формата — `StructuredOutputError` наверх."""
    messages = [
        ChatMessage(role="system", content=load_prompt("praise", prompt_version)),
        ChatMessage(role="user", content=_facts(data)),
    ]
    return await chat_structured(client, messages, PraiseOutput, model=model)


def _facts(data: PraiseInput) -> str:
    parts = [f"Класс ученика: {data.grade}"] if data.grade is not None else []
    for task in data.tasks:
        steps = "\n".join(task.steps[:MAX_STEPS])
        lines = [f"Задание index={task.index}"]
        if task.condition.strip():
            lines.append(f"Условие: {task.condition.strip()[:MAX_CONDITION_CHARS]}")
        lines.append(f"Решение ученика:\n{steps}")
        if (task.answer or "").strip():
            lines.append(f"Ответ ученика: {task.answer}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def accepted_praise(data: PraiseInput, output: PraiseOutput) -> dict[int, str]:
    """Тексты, прошедшие проверку, по индексу задания; остальным вызывающий даёт запасной.

    Индекс не из присланного списка отбрасывается: это могло бы оказаться задание с ошибкой.
    """
    tasks = {task.index: task for task in data.tasks}
    texts: dict[int, str] = {}
    seen: set[int] = set()
    for item in output.items:
        task = tasks.get(item.index)
        if task is None or item.index in seen:
            continue
        seen.add(item.index)  # второй текст на то же задание не заменяет первый
        text = praise_text(item)
        if text is not None and is_safe(text, task):
            texts[item.index] = text
    return texts


def praise_text(item: PraiseItem) -> str | None:
    """Строка для сводки: «почему» и приём; None — модель не заполнила одно из полей."""
    why = _SPACES.sub(" ", item.why).strip()
    technique = _SPACES.sub(" ", item.technique).strip().rstrip(".!").strip()
    if not why or not technique:
        return None
    if why[-1] not in ".!?":
        why += "."
    return f"{why} Приём — {technique[0].lower()}{technique[1:]}."


def is_safe(text: str, task: PraiseTask) -> bool:
    if len(text) > MAX_PRAISE_CHARS or _ERROR_MARKER.search(text):
        return False
    known = _known_numbers(task)
    mentioned = [parse_value(token) for token in _NUMBER.findall(text)]
    return all(value is not None and value in known for value in mentioned)


def _known_numbers(task: PraiseTask) -> set[Any]:
    """Числа, которые ребёнок видит в своём задании: условие, его шаги, его ответ."""
    written = "\n".join([task.condition, *task.steps, task.answer or ""])
    tokens = [*_NUMBER.findall(written), *_NUMBER_PART.findall(written), *_DIGITS.findall(written)]
    values = {parse_value(token) for token in tokens}
    return values - {None}


def describable(grade: GradeResult, steps: list[str]) -> bool:
    """Есть что описывать: шаги записаны, и пересчёт не нашёл в них расхождений.

    Описка при верном ответе и обрывки деления уголком — верное задание с неверными строками:
    модель повторила бы их ребёнку как правильные.
    """
    clean = not any(c.status == "mismatch" or c.doubtful for c in grade.line_checks)
    return clean and any(step.strip() for step in steps)


def fallback_praise(grade: GradeResult) -> str:
    """Текст без модели — только то, что установил валидатор."""
    verified = sum(1 for c in grade.line_checks if c.status == "ok")
    clean = not any(c.status == "mismatch" or c.doubtful for c in grade.line_checks)
    if not clean or verified == 0:
        return FALLBACK_ANSWER
    if verified == 1:
        recount = "Пересчитал твоё действие — сходится"
    else:
        recount = f"Пересчитал {verified} {_actions(verified)} — все сходятся"
    return f"{recount}, и ответ верный." if grade.answers_match else f"{recount}."


def _actions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "действие"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "действия"
    return "действий"
