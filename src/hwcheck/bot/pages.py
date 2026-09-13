"""Сценарий «фото учебника + фото тетради» (сессия 9).

Учебник — печатные условия без решения ученика; тетрадь — решение, часто без
условия или с краткой записью. Роль страницы определяется детерминированно по
результату vision (без лишнего вызова LLM), условия сопоставляются по номеру
задания: №19 из тетради получает условие №19 из учебника. Печатное условие
надёжнее краткой записи ученика, поэтому учебник всегда в приоритете.
"""

import re
import time
from typing import Literal

from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.validator import check_steps

PageRole = Literal["textbook", "notebook", "empty"]

# больше фото за одно сообщение не обрабатываем: каждое — vision-вызов (~4k токенов)
MAX_PHOTOS = 4
# условия учебника живут одну «домашку»: у другого задания через день те же номера
TEXTBOOK_TTL_S = 60 * 60


def _computed(task: VisionTask) -> bool:
    """Есть строка «число = число», которую валидатор смог пересчитать."""
    return any(c.status in ("ok", "mismatch") for c in check_steps(task.student_solution_steps))


def page_role(page: VisionPage | None) -> PageRole:
    """Тетрадь — «Ответ:» или вычисленное равенство; учебник — условия без того и другого.

    Структуризатор может сложить печатные выражения («12 + x = 12», «803 + 169»)
    в строки любого задания учебника, поэтому считать строки бессмысленно:
    признак работы ученика — записанный ответ или равенство с числом по обе
    стороны. Но и одна случайная такая строка на странице с семью условиями не
    делает её тетрадью (живой тест, сессия 9): признаков должно быть не меньше
    половины числа условий. Страница без единого условия, но со строками —
    тетрадь (решённые уравнения без «Ответ:»).
    """
    if page is None or not page.tasks:
        return "empty"
    tasks = page.tasks
    signals = sum(1 for t in tasks if (t.student_answer or "").strip() or _computed(t))
    n_cond = sum(1 for t in tasks if t.task_text.strip())
    if signals and 2 * signals >= n_cond:
        return "notebook"
    if n_cond == 0 and any(t.student_solution_steps for t in tasks):
        return "notebook"
    return "textbook" if n_cond else "empty"


# номер задания в транскрипции: «№ 13», «N 462», «N° 35», «Задание 7», «Упр. 5» или
# «23.» в начале строки. «1.124» — не номер 1, «1)» — пункт задания, а не номер
_TASK_NUMBER = re.compile(
    r"(?:(?<![A-Za-zА-Яа-яЁё])(?:№|N[°º]?|задани[ея]|упр(?:ажнение)?\.?)\s*(\d{1,4})\b"
    r"|^\s*(\d{1,4})\.(?!\d))",
    re.IGNORECASE | re.MULTILINE,
)


def written_numbers(transcript: str) -> set[int]:
    return {int(m.group(1) or m.group(2)) for m in _TASK_NUMBER.finditer(transcript)}


def mark_written_numbers(page: VisionPage, transcript: str) -> VisionPage:
    """Номер, которого нет в транскрипции, придумал структуризатор («нумеруй с 1»)."""
    written = written_numbers(transcript)
    tasks = [t.model_copy(update={"number_on_page": t.number in written}) for t in page.tasks]
    return page.model_copy(update={"tasks": tasks})


def textbook_is_fresh(saved_at: float | None) -> bool:
    return saved_at is not None and time.time() - saved_at < TEXTBOOK_TTL_S


def _condition_of(task: VisionTask) -> str:
    """У печатного задания вроде «21. 15 · 10 + (30 − 20) · 5» условие — само выражение."""
    return task.task_text.strip() or "; ".join(task.student_solution_steps).strip()


def merge_textbook(known: list[VisionTask], new: list[VisionTask]) -> list[VisionTask]:
    """Условия по номеру; новая страница учебника перекрывает старую по тем же номерам."""
    by_number = {t.number: t for t in known}
    for task in new:
        condition = _condition_of(task)
        if condition:
            by_number[task.number] = task.model_copy(
                update={
                    "task_text": condition,
                    "student_solution_steps": [],
                    "student_answer": None,
                }
            )
    return [by_number[n] for n in sorted(by_number)]


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
CONTENT_MATCH_MIN = 2  # столько отличительных чисел нужно, чтобы поверить совпадению без номера


def _numbers(*texts: str) -> set[str]:
    return {m.group(0).replace(",", ".") for text in texts for m in _NUMBER.finditer(text)}


def _distinctive_numbers(textbook: list[VisionTask]) -> dict[int, set[str]]:
    """Числа-«подписи» условия: от двух цифр и встречаются ровно в одном условии страницы.

    «2» и «10» есть в половине задач начальной школы — по ним сопоставлять нельзя;
    «220», «180», «700» из одной задачи — надёжная подпись.
    """
    per_task = {
        t.number: {n for n in _numbers(t.task_text) if len(n.replace(".", "")) >= 2}
        for t in textbook
    }
    counts: dict[str, int] = {}
    for numbers in per_task.values():
        for n in numbers:
            counts[n] = counts.get(n, 0) + 1
    return {number: {n for n in numbers if counts[n] == 1} for number, numbers in per_task.items()}


def attach_conditions(notebook: list[VisionTask], textbook: list[VisionTask]) -> list[VisionTask]:
    """Заданию тетради подставляется печатное условие учебника.

    Балл кандидата: совпадение номера — 2, каждое общее отличительное число — 1;
    порог CONTENT_MATCH_MIN. Номер считается, только если он записан на обеих
    страницах: придуманные структуризатором «1, 2, 3» совпадают у любых двух страниц
    (живые логи 13.09). Рукописный номер читается ненадёжно («№19» → 29), поэтому
    совпадение по числам условия допустимо, но одно условие достаётся только одному
    заданию тетради (кроме точного совпадения номера); при равенстве баллов — номер.
    """
    candidates = [t for t in textbook if t.task_text.strip()]
    distinctive = _distinctive_numbers(candidates)
    scored: list[tuple[int, int, int, VisionTask]] = []
    for i, task in enumerate(notebook):
        student = _numbers(task.task_text, *task.student_solution_steps)
        for candidate in candidates:
            exact = int(
                candidate.number == task.number and candidate.number_on_page and task.number_on_page
            )
            score = 2 * exact + len(student & distinctive[candidate.number])
            if score >= CONTENT_MATCH_MIN:
                scored.append((score, exact, i, candidate))
    chosen: dict[int, VisionTask] = {}
    taken: set[int] = set()
    for _score, exact, i, candidate in sorted(scored, key=lambda x: (-x[0], -x[1], x[2])):
        if i in chosen or (candidate.number in taken and not exact):
            continue
        chosen[i] = candidate
        taken.add(candidate.number)
    return [
        task.model_copy(update={"task_text": chosen[i].task_text, "number": chosen[i].number})
        if i in chosen
        else task
        for i, task in enumerate(notebook)
    ]


def format_numbers(numbers: list[int]) -> str:
    """«№16–22» для сплошного диапазона из трёх и более, иначе «№3, №7»."""
    ordered = sorted(set(numbers))
    if len(ordered) >= 3 and ordered[-1] - ordered[0] == len(ordered) - 1:
        return f"№{ordered[0]}–{ordered[-1]}"
    return ", ".join(f"№{n}" for n in ordered)
