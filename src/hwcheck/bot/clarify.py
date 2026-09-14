"""Уточняющие вопросы ученику, шаг 1: «не уверен» с понятной причиной → вопрос → пересчёт.

Спрашиваем только то, что ответ ребёнка действительно проясняет: итоговый ответ, которого нет
или который не разобран, неразборчивый знак или строку. Вопрос не показывает эталон и не
предлагает вариантов с правильным числом; ответ проверяется детерминированно, как исходная
запись. На ложные ошибки распознавания («99» вместо «999») — шаг 2, не здесь.
"""

import re

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import CheckedTask, Clarification
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.pages import task_label
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.mathparse import parse_value
from hwcheck.pipeline.schemas import VisionTask

MAX_QUESTIONS = 2  # на одну домашку: больше — трение вместо помощи
MAX_ATTEMPTS = 2  # неразобранный ответ: одна подсказка, затем оставляем как есть

_UNREADABLE = re.compile(r"<\s*неразборчиво\s*>", re.IGNORECASE)
# неразборчивое между числами или скобками — это знак действия
_UNREADABLE_SIGN = re.compile(r"[\d)]\s*<\s*неразборчиво\s*>\s*[\d(]", re.IGNORECASE)
# кнопка → (что видит ребёнок, что пишем в строку для пересчёта)
SIGNS: dict[str, tuple[str, str]] = {
    "plus": ("+", "+"),
    "minus": ("−", "-"),
    "mul": ("·", "*"),
    "div": (":", ":"),
}
_TYPED_SIGNS = {"+": "plus", "-": "minus", "−": "minus", "*": "mul", "·": "mul", "×": "mul",
                ":": "div", "÷": "div", "/": "div"}  # fmt: skip


def plan_clarifications(tasks: list[CheckedTask]) -> list[Clarification]:
    plan: list[Clarification] = []
    for index, item in enumerate(tasks):
        clarification = _clarification_for(index, item)
        if clarification is not None:
            plan.append(clarification)
        if len(plan) == MAX_QUESTIONS:
            break
    return plan


def _clarification_for(index: int, item: CheckedTask) -> Clarification | None:
    if item.grade.verdict != "uncertain":
        return None
    reason = item.grade.uncertain_reason
    if reason == "unreadable":
        for line_index, line in enumerate(item.task.student_solution_steps):
            if _UNREADABLE_SIGN.search(line):
                return Clarification(task_index=index, kind="sign", line_index=line_index)
            if _UNREADABLE.search(line):
                return Clarification(task_index=index, kind="line", line_index=line_index)
        # неразборчив сам ответ — спрашиваем ответ, если есть с чем сверить
        return Clarification(task_index=index, kind="answer") if item.ref is not None else None
    if reason in ("no_answer", "answer_unparseable") and item.ref is not None:
        return Clarification(task_index=index, kind="answer")
    return None


def question(item: CheckedTask, clarification: Clarification) -> tuple[str, Buttons | None]:
    label = task_label(item.task)
    if clarification.kind == "answer":
        if item.grade.uncertain_reason == "no_answer":
            return f"{label}: не нашёл итоговый ответ. Какой ответ у тебя получился?", None
        return f"{label}: не разобрал ответ. Напиши его числом, как в тетради.", None
    line = _line(item, clarification)
    if clarification.kind == "sign":
        shown = _UNREADABLE.sub("?", line, count=1)
        text = f"{label}, строка {_line_number(clarification)}: {shown}\nКакой знак вместо «?»?"
        buttons = [
            [
                callback_button(shown_sign, f"clarify:{key}")
                for key, (shown_sign, _) in SIGNS.items()
            ]
        ]
        return text, buttons
    return (
        f"{label}: не разобрал строку {_line_number(clarification)}. "
        "Перепиши её, как в тетради, со знаком «=».",
        None,
    )


def retry_prompt(clarification: Clarification) -> tuple[str, Buttons | None]:
    if clarification.kind == "answer":
        return "Не понял 🙂 Напиши только число, без слов.", None
    if clarification.kind == "line":
        return "Не понял 🙂 Перепиши строку целиком, со знаком «=».", None
    buttons = [[callback_button(shown, f"clarify:{key}") for key, (shown, _) in SIGNS.items()]]
    return "Нажми кнопку со знаком 👇", buttons


def apply_text(item: CheckedTask, clarification: Clarification, text: str) -> CheckedTask | None:
    """Ответ текстом; None — не понятно, что имел в виду ребёнок."""
    if clarification.kind == "answer":
        if parse_value(text) is None:
            return None
        return regrade(item, item.task.model_copy(update={"student_answer": text.strip()}))
    if clarification.kind == "sign":
        key = _TYPED_SIGNS.get(text.strip())
        return apply_sign(item, clarification, key) if key else None
    if "=" not in text:
        return None
    return _replace_line(item, clarification, text.strip())


def apply_sign(item: CheckedTask, clarification: Clarification, key: str) -> CheckedTask | None:
    """Кнопка знака; payload недоверенный — неизвестный ключ означает None."""
    if clarification.kind != "sign" or key not in SIGNS:
        return None
    line = _UNREADABLE.sub(SIGNS[key][1], _line(item, clarification), count=1)
    return _replace_line(item, clarification, line)


def regrade(item: CheckedTask, task: VisionTask) -> CheckedTask:
    if item.ref is not None:
        result = grade(
            task.student_solution_steps, task.student_answer, item.ref, condition=task.task_text
        )
    else:
        result = validator_only_grade(task.student_solution_steps, condition=task.task_text)
    return CheckedTask(task=task, ref=item.ref, grade=result)


def _replace_line(item: CheckedTask, clarification: Clarification, line: str) -> CheckedTask:
    steps = list(item.task.student_solution_steps)
    steps[clarification.line_index or 0] = line
    return regrade(item, item.task.model_copy(update={"student_solution_steps": steps}))


def _line(item: CheckedTask, clarification: Clarification) -> str:
    return item.task.student_solution_steps[clarification.line_index or 0]


def _line_number(clarification: Clarification) -> int:
    return (clarification.line_index or 0) + 1
