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
from hwcheck.subjects.base import Finding
from hwcheck.subjects.math.module import findings_from_grade

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
_TYPED_YES_NO = {"да": "yes", "нет": "no"}
# kind находок, которые пересчитывает findings_from_grade (regrade их заменяет, не находки других
# предметов вроде word — код-ревью 17.09: пересчёт не должен терять несвязанные находки)
MATH_KINDS = {"arithmetic", "uncertain"}


def plan_clarifications(tasks: list[CheckedTask]) -> list[Clarification]:
    plan: list[Clarification] = []
    for index, item in enumerate(tasks):
        clarification = _clarification_for(index, item)
        if clarification is not None:
            plan.append(clarification)
        if len(plan) == MAX_QUESTIONS:
            return plan
    for index, item in enumerate(tasks):
        for finding in item.findings:
            if len(plan) == MAX_QUESTIONS:
                return plan
            if _is_word_candidate(finding):
                plan.append(Clarification(task_index=index, kind="word", finding_id=finding.id))
    return plan


def _is_word_candidate(finding: Finding) -> bool:
    return (
        finding.strength == "candidate"
        and finding.word is not None
        and finding.word.box is not None
        and finding.confirmed is None
    )


def _clarification_for(index: int, item: CheckedTask) -> Clarification | None:
    if item.grade.verdict != "uncertain":
        return None
    reason = item.grade.uncertain_reason
    # итоговый ответ спрашиваем только при хоть одной сошедшейся строке решения:
    # иначе вопрос — способ подобрать ответ без решения
    worked = any(check.status == "ok" for check in item.grade.line_checks)
    can_ask_answer = item.ref is not None and worked
    if reason == "unreadable":
        for line_index, line in enumerate(item.task.student_solution_steps):
            if _UNREADABLE_SIGN.search(line):
                return Clarification(task_index=index, kind="sign", line_index=line_index)
            if _UNREADABLE.search(line):
                return Clarification(task_index=index, kind="line", line_index=line_index)
        # неразборчив сам ответ — спрашиваем ответ, если есть с чем сверить
        return Clarification(task_index=index, kind="answer") if can_ask_answer else None
    if reason in ("no_answer", "answer_unparseable") and can_ask_answer:
        return Clarification(task_index=index, kind="answer")
    return None


def question(item: CheckedTask, clarification: Clarification) -> tuple[str, Buttons | None]:
    label = task_label(item.task)
    if clarification.kind == "answer":
        if item.grade.uncertain_reason == "no_answer":
            return f"{label}: не нашёл итоговый ответ. Какой ответ у тебя получился?", None
        return f"{label}: не разобрал ответ. Напиши его числом, как в тетради.", None
    if clarification.kind == "word":
        finding = _finding(item, clarification)
        if finding is None:
            # regrade/ответ на другой вопрос успели снять находку — вызывающий код должен был
            # отфильтровать такой вопрос через _finding раньше, а не звать question()
            # (handlers._ask_clarification)
            raise ValueError("word clarification without a live finding")
        word = finding.word.text if finding.word is not None else ""
        return f"{label}: здесь написано «{finding.actual or word}»?", word_buttons(clarification)
    line = _line(item, clarification)
    if clarification.kind == "sign":
        shown = _UNREADABLE.sub("?", line, count=1)
        text = f"{label}, строка {_line_number(clarification)}: {shown}\nКакой знак вместо «?»?"
        return text, sign_buttons(clarification)
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
    if clarification.kind == "word":
        return "Нажми «Да» или «Нет» 👇", word_buttons(clarification)
    return "Нажми кнопку со знаком 👇", sign_buttons(clarification)


def sign_buttons(clarification: Clarification) -> Buttons:
    token = clarification.token
    return [[callback_button(shown, f"clarify:{token}:{key}") for key, (shown, _) in SIGNS.items()]]


def word_buttons(clarification: Clarification) -> Buttons:
    token = clarification.token
    return [
        [
            callback_button("Да", f"clarify:{token}:yes"),
            callback_button("Нет", f"clarify:{token}:no"),
        ]
    ]


def parse_sign_payload(payload: str) -> tuple[str, str] | None:
    """`clarify:<token>:<знак>` → (token, знак); payload недоверенный."""
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "clarify":
        return None
    return parts[1], parts[2]


def apply_text(item: CheckedTask, clarification: Clarification, text: str) -> CheckedTask | None:
    """Ответ текстом; None — не понятно, что имел в виду ребёнок."""
    if clarification.kind == "answer":
        if parse_value(text) is None:
            return None
        return regrade(
            item,
            item.task.model_copy(update={"student_answer": text.strip()}),
            clarification.task_index,
        )
    if clarification.kind == "sign":
        key = _TYPED_SIGNS.get(text.strip())
        return apply_sign(item, clarification, key) if key else None
    if clarification.kind == "word":
        key = _TYPED_YES_NO.get(text.strip().lower())
        return apply_word(item, clarification, key) if key else None
    if "=" not in text:
        return None
    return _replace_line(item, clarification, text.strip())


def apply_sign(item: CheckedTask, clarification: Clarification, key: str) -> CheckedTask | None:
    """Кнопка знака; payload недоверенный — неизвестный ключ означает None."""
    if clarification.kind != "sign" or key not in SIGNS:
        return None
    line = _UNREADABLE.sub(SIGNS[key][1], _line(item, clarification), count=1)
    return _replace_line(item, clarification, line)


def apply_word(item: CheckedTask, clarification: Clarification, key: str) -> CheckedTask | None:
    """Ответ «здесь написано …?»; payload/текст недоверенные — не yes/no означает None."""
    if clarification.kind != "word" or key not in ("yes", "no"):
        return None
    findings = list(item.findings)
    index = next((i for i, f in enumerate(findings) if f.id == clarification.finding_id), None)
    if index is None:
        return None
    findings[index] = findings[index].model_copy(update={"confirmed": key == "yes"})
    return item.model_copy(update={"findings": findings})


def regrade(item: CheckedTask, task: VisionTask, task_index: int) -> CheckedTask:
    if item.ref is not None:
        result = grade(
            task.student_solution_steps, task.student_answer, item.ref, condition=task.task_text
        )
    else:
        result = validator_only_grade(task.student_solution_steps, condition=task.task_text)
    findings = _merge_findings(item.findings, findings_from_grade(task_index, result))
    return CheckedTask(
        task=task,
        ref=item.ref,
        grade=result,
        findings=findings,
        ref_status=item.ref_status,  # эталон не пересчитывается — статус остаётся тем же
    )


def _merge_findings(old: list[Finding], new_math: list[Finding]) -> list[Finding]:
    """Пересчёт даёт только математические находки (`findings_from_grade`, не больше одной):
    они встают на место прежней математической находки, прочие (например, word из другого
    предмета) остаются на своих местах; без прежней математической находки — новые добавляются
    в конец; если пересчёт не дал ни одной — прежняя просто пропадает (найденная ошибка ушла)."""
    merged: list[Finding] = []
    inserted = False
    for finding in old:
        if finding.kind in MATH_KINDS:
            if not inserted:
                merged.extend(new_math)
                inserted = True
        else:
            merged.append(finding)
    if not inserted:
        merged.extend(new_math)
    return merged


def _replace_line(item: CheckedTask, clarification: Clarification, line: str) -> CheckedTask:
    steps = list(item.task.student_solution_steps)
    steps[_line_index(clarification)] = line
    return regrade(
        item,
        item.task.model_copy(update={"student_solution_steps": steps}),
        clarification.task_index,
    )


def _line(item: CheckedTask, clarification: Clarification) -> str:
    return item.task.student_solution_steps[_line_index(clarification)]


def _line_number(clarification: Clarification) -> int:
    return _line_index(clarification) + 1


def _line_index(clarification: Clarification) -> int:
    if clarification.line_index is None:
        raise ValueError(f"clarification {clarification.kind!r} without line_index")
    return clarification.line_index


def _finding(item: CheckedTask, clarification: Clarification) -> Finding | None:
    """Находка `word`-вопроса — если она ещё живая (кандидат без ответа, со словом), иначе None:
    её могли снять пересчётом другого вопроса той же задачи или уже ответить на неё раньше.

    Ищем по `Finding.id`: пересчёт меняет состав списка находок (`_merge_findings`), поэтому
    позиция в нём — не ссылка."""
    if clarification.finding_id is None:
        return None
    finding = next((f for f in item.findings if f.id == clarification.finding_id), None)
    if finding is None:
        return None
    if finding.word is None or finding.strength != "candidate" or finding.confirmed is not None:
        return None
    return finding
