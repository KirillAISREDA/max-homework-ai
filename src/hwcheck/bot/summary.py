"""Сводка ученику из находок (спецификация каркаса §8): одна форма для всех предметов.

Строка задания — по худшей находке: верно / есть ошибка / стоит перепроверить / разобрал без
оценки. Кнопка «Разобрать» — только для ошибок (verified или подтверждённый candidate).
"""

from __future__ import annotations

from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.pages import task_label
from hwcheck.subjects.base import Finding, strength_of_task
from hwcheck.subjects.math.module import findings_from_grade


def task_findings(index: int, item: CheckedTask) -> list[Finding]:
    """Находки модуля; запасной вывод из `grade` — только у предмета с пересчётом (математика)."""
    if item.grade is None:
        return item.findings
    return item.findings or findings_from_grade(index, item.grade)


def task_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    label = task_label(item.task)
    findings = task_findings(index, item)
    strength = strength_of_task(findings)
    if strength == "ok":
        return f"{label} — верно ✅", None
    if strength == "verified":
        error = next(f for f in findings if f.is_error)
        button = [callback_button(f"Разобрать {lower(label)}", f"tutor:{index}")]
        return f"{label} — есть ошибка{_where(error)} ❌", button
    if strength == "candidate":
        candidates = [f for f in findings if f.strength == "candidate" and f.confirmed is None]
        if len(candidates) == 1 and candidates[0].detail:
            return f"{label} — {candidates[0].detail} 🤔", None
        return f"{label} — стоит перепроверить 🤔 ({_places(len(candidates))})", None
    return f"{label} — разобрал, оценки нет 📝", None


def clarified_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    """Итог после ответа ученика: «не уверен» здесь — ответ понят, но проверка не сошлась."""
    if strength_of_task(task_findings(index, item)) == "candidate":
        return f"{task_label(item.task)} — спасибо, но и так не получилось проверить 🤔", None
    return task_line(index, item)


def review_header(state: ChatState) -> str:
    correct = sum(
        1 for i, t in enumerate(state.tasks) if strength_of_task(task_findings(i, t)) == "ok"
    )
    return f"Проверил! {correct} из {len(state.tasks)} верно.\n"


def remaining_buttons(state: ChatState) -> Buttons:
    """Кнопки для ещё не разобранных ошибок."""
    return [
        [callback_button(f"Разобрать {lower(task_label(t.task))}", f"tutor:{i}")]
        for i, t in enumerate(state.tasks)
        if strength_of_task(task_findings(i, t)) == "verified" and i not in state.resolved_indices
    ]


def lower(label: str) -> str:
    """«Задание 1» посреди фразы: «Разобрать задание 1»; «№19» не меняется."""
    return label[:1].lower() + label[1:]


def _where(finding: Finding) -> str:
    if finding.line is not None and finding.kind == "arithmetic":
        return f" (строка {finding.line})"
    if finding.actual:
        return f" (слово «{finding.actual}»)"
    return ""


def _places(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} место"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} места"
    return f"{n} мест"
