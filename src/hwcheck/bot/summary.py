"""Сводка ученику из находок (спецификация каркаса §8): одна форма для всех предметов.

Строка задания — по худшей находке: верно / есть ошибка / стоит перепроверить / разобрал без
оценки. Кнопка «Разобрать» — только для ошибок (verified или подтверждённый candidate).
К верному заданию дописывается объяснение, за что похвалили (`CheckedTask.praise`). У задания из
пунктов («а)», «б)») ошибка называется пунктом, а остальные пункты перечисляются по итогу: «есть
ошибка в пункте а) ❌ Верно: б), в).» (живая проверка 30.09).
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.pages import task_label
from hwcheck.pipeline.mathparse import school_notation
from hwcheck.pipeline.reading import line_label
from hwcheck.pipeline.validator import LineCheck
from hwcheck.subjects.base import Finding, strength_of_task
from hwcheck.subjects.math.module import findings_from_grade

MAX_MESSAGE_CHARS = 4000  # лимит текста сообщения MAX
QUOTE_CHARS = 30  # столько знаков строки ребёнка цитируем, если пункта нет
# сводка без ошибок и без вопросов — ребёнок не должен гадать, что дальше (живая проверка 30.09)
NEXT_PHOTO = "Пришли фото следующего задания — проверю 📸"

ItemStatus = Literal["error", "doubt", "ok", "unknown"]
# как перечисляются пункты после главной фразы строки, по порядку
_ITEM_GROUPS: tuple[tuple[ItemStatus, str], ...] = (
    ("doubt", "Под вопросом"),
    ("ok", "Верно"),
    ("unknown", "Не проверил"),
)


def task_findings(index: int, item: CheckedTask) -> list[Finding]:
    """Находки модуля; запасной вывод из `grade` — только у предмета с пересчётом (математика)."""
    if item.grade is None:
        return item.findings
    return item.findings or findings_from_grade(index, item.grade)


def task_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    line, button = verdict_line(index, item)
    praise = praise_of(index, item)
    return (f"{line} {praise}" if praise else line), button


def praise_of(index: int, item: CheckedTask) -> str | None:
    """Объяснение показываем, только пока задание верно: текст мог остаться от прошлого вердикта."""
    if not item.praise or strength_of_task(task_findings(index, item)) != "ok":
        return None
    return item.praise


def verdict_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    label = task_label(item.task)
    findings = task_findings(index, item)
    strength = strength_of_task(findings)
    if strength == "ok":
        return f"{label} — верно ✅", None
    items = _items(item)
    if strength == "verified":
        error = next(f for f in findings if f.is_error)
        button = [callback_button(f"Разобрать {lower(label)}", f"tutor:{index}")]
        wrong = [f"{name})" for name, status in items if status == "error"]
        if wrong and error.kind == "arithmetic":
            head = (
                f"есть ошибка в пункте {wrong[0]}"
                if len(wrong) == 1
                else f"есть ошибки в пунктах {', '.join(wrong)}"
            )
            return f"{label} — {head} ❌{_other_items(items)}", button
        return f"{label} — есть ошибка{_where(item, error)} ❌", button
    if strength == "candidate":
        candidates = [f for f in findings if f.strength == "candidate" and f.confirmed is None]
        if len(candidates) == 1 and candidates[0].detail:
            return f"{label} — {candidates[0].detail} 🤔{_other_items(items)}", None
        return f"{label} — стоит перепроверить 🤔 ({_places(len(candidates))})", None
    return f"{label} — разобрал, оценки нет 📝", None


def clarified_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    """Итог после ответа ученика: «не уверен» здесь — ответ понят, но проверка не сошлась."""
    if strength_of_task(task_findings(index, item)) == "candidate":
        return f"{task_label(item.task)} — спасибо, но и так не получилось проверить 🤔", None
    return task_line(index, item)


def review_header(state: ChatState) -> str:
    """Счёт — по заданиям с вердиктом: непрочитанное задание не делает «0 из 2 верно» из «0 из 1»
    (живая проверка 30.09); без единого вердикта — без счёта, строки заданий скажут остальное."""
    strengths = [strength_of_task(task_findings(i, t)) for i, t in enumerate(state.tasks)]
    correct = strengths.count("ok")
    graded = correct + strengths.count("verified")
    return f"Проверил! {correct} из {graded} верно.\n" if graded else "Проверил!\n"


def review_message(state: ChatState) -> tuple[str, Buttons]:
    """Текст сводки и кнопки «Разобрать»; задания с вопросом ученику ждут его ответа."""
    asked = Counter(c.task_index for c in state.clarifications)
    verdicts: list[str] = []
    praises: list[str | None] = []
    buttons: Buttons = []
    for index, item in enumerate(state.tasks):
        if index in asked:
            details = {1: "одну деталь", 2: "пару деталей"}.get(asked[index], "несколько деталей")
            verdicts.append(f"{task_label(item.task)} — уточню у тебя {details} ✍️")
            praises.append(None)
            continue
        line, button = verdict_line(index, item)
        verdicts.append(line)
        praises.append(praise_of(index, item))
        if button:
            buttons.append(button)
    footer = "" if buttons or asked else f"\n{NEXT_PHOTO}"
    return _fit(review_header(state), verdicts, praises, footer), buttons


def _fit(header: str, verdicts: list[str], praises: list[str | None], footer: str = "") -> str:
    """Сводка в пределах лимита сообщения: объяснения опускаются с конца, вердикты остаются все —
    без объяснения ребёнок обойдётся, без вердикта нет."""
    kept = list(praises)
    while True:
        lines = [
            f"{verdict} {praise}" if praise else verdict
            for verdict, praise in zip(verdicts, kept, strict=True)
        ]
        text = header + "\n".join(lines) + footer
        explained = [i for i, praise in enumerate(kept) if praise]
        if message_length(text) <= MAX_MESSAGE_CHARS or not explained:
            return text
        kept[explained[-1]] = None


def message_length(text: str) -> int:
    """Длина в элементах UTF-16: эмодзи вроде 🤔 занимают два. Как считает MAX, документация
    не говорит — берём счёт с запасом, чтобы сообщение точно прошло лимит."""
    return len(text.encode("utf-16-le")) // 2


def remaining_buttons(state: ChatState) -> Buttons:
    """Кнопки для ещё не разобранных ошибок (решение, показанное кодом, — тоже разбор)."""
    done = {*state.resolved_indices, *state.shown_indices}
    return [
        [callback_button(f"Разобрать {lower(task_label(t.task))}", f"tutor:{i}")]
        for i, t in enumerate(state.tasks)
        if strength_of_task(task_findings(i, t)) == "verified" and i not in done
    ]


def lower(label: str) -> str:
    """«Задание 1» посреди фразы: «Разобрать задание 1»; «№19» не меняется."""
    return label[:1].lower() + label[1:]


def _where(item: CheckedTask, finding: Finding) -> str:
    """Где ошибка: пункт строки, а без пункта — сама строка ребёнка; «строка 2» ребёнку ничего не
    говорит (живая проверка 30.09)."""
    steps = item.task.student_solution_steps
    if finding.line is not None and finding.kind == "arithmetic" and finding.line <= len(steps):
        line = steps[finding.line - 1]
        label = line_label(line)
        if label is not None:
            return f" в пункте {label})"
        shown = school_notation(line.strip())
        if len(shown) > QUOTE_CHARS:
            shown = shown[:QUOTE_CHARS].rstrip() + "…"
        return f" в строке «{shown}»"
    if finding.actual:
        return f" (слово «{finding.actual}»)"
    return ""


def _items(item: CheckedTask) -> list[tuple[str, ItemStatus]]:
    """Итог по пунктам задания; строки без метки после пункта — его действия. Пустой список —
    пунктов меньше двух или метка повторилась (тогда пункты не различить)."""
    if item.grade is None:
        return []
    groups: dict[str, list[LineCheck]] = {}
    current: str | None = None
    for check in item.grade.line_checks:
        label = line_label(check.line)
        if label is not None:
            if label in groups:
                return []
            current = label
            groups[label] = []
        if current is not None:
            groups[current].append(check)
    if len(groups) < 2:
        return []
    return [(label, _item_status(checks)) for label, checks in groups.items()]


def _item_status(checks: list[LineCheck]) -> ItemStatus:
    if any(c.status == "mismatch" for c in checks):
        return "error"
    if any(c.misread for c in checks):
        return "doubt"
    if all(c.status == "ok" for c in checks):
        return "ok"
    return "unknown"


def _other_items(items: list[tuple[str, ItemStatus]]) -> str:
    """« Верно: б), в). Не проверил: г).» — без пунктов с ошибкой: они в главной фразе строки.
    Ни один пункт не проверен — перечислять нечего."""
    if all(status == "unknown" for _name, status in items):
        return ""
    parts = []
    for status, title in _ITEM_GROUPS:
        names = [f"{name})" for name, current in items if current == status]
        if names:
            parts.append(f"{title}: {', '.join(names)}.")
    return " " + " ".join(parts) if parts else ""


def _places(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} место"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} места"
    return f"{n} мест"
