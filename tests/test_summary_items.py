"""Сводка по пунктам (кусок 3 разбора живой проверки 30.09, спецификация
2026-09-30-summary-items-design.md).

Тестер: «где он написал ЕСТЬ ОШИБКА стр 2 — указать бы точнее, что ошибка в п. а). Остальное решено
верно». Плюс «Задание 2181» вместо «№2.181», «0 из 2 верно» при нечитаемом втором задании и тишина
после сводки без ошибок.
"""

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask, Clarification
from hwcheck.bot.pages import mark_written_numbers, task_label, written_numbers
from hwcheck.bot.summary import NEXT_PHOTO, review_header, review_message, task_line
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from test_reading import CONDITION_2181, STEPS_2181

ITEMS_2181 = [
    "а) 39452 - 16452 : (300 - 264) = 38997",
    "б) 2558304 : 63 + 1662372 : 61 = 67860",
    "в) 93601 - 601 * (231 - 88) = 7658",
    "г) 329503 + 12146 * 28 + 715 * 449 =",
]


def checked(
    steps: list[str], *, number: int = 2181, condition: str = "", label: str | None = "2.181"
) -> CheckedTask:
    task = VisionTask(
        number=number,
        task_text=condition,
        student_solution_steps=steps,
        confidence=1,
        number_on_page=label is not None,
        number_label=label,
    )
    return CheckedTask(
        task=task, ref=None, grade=validator_only_grade(steps, condition=condition or None)
    )


# --- номер с точкой ---


def test_dotted_number_is_written_on_page() -> None:
    assert written_numbers("Домашняя работа\n№ 2.181\na) 9454 - 16452") == {2181}
    assert written_numbers("2.177 Назовите уменьшаемое\n2.178 Запишите разность") == {2177, 2178}
    assert written_numbers("№ 13\n15 * 10 = 150") == {13}
    assert written_numbers("12.5 + 3 = 15.5\n12.75 + 1 = 13.75\n12.50\n0.25 кг") == set()


def test_dotted_number_keeps_its_label() -> None:
    task = VisionTask(number=2181, task_text="", confidence=1)
    page = mark_written_numbers(VisionPage(tasks=[task], page_ok=True), "№ 2.181\nа) 5 + 3 = 8")
    [marked] = page.tasks
    assert (marked.number_on_page, marked.number_label) == (True, "2.181")
    assert task_label(marked) == "№2.181"


def test_number_label_is_not_asked_from_the_model() -> None:
    # номер с точкой проставляет код по транскрипции — в схеме ответа структуризатора его нет
    assert "number_label" not in VisionPage.model_json_schema()["$defs"]["VisionTask"]["properties"]


# --- строка задания по пунктам ---


def test_error_line_names_the_item_and_the_rest() -> None:
    line, button = task_line(0, checked(ITEMS_2181, condition=CONDITION_2181))
    assert line == "№2.181 — есть ошибка в пункте а) ❌ Верно: б), в). Не смог проверить: г)."
    assert button is not None and button[0]["text"] == "Разобрать №2.181"


def test_several_errors_are_listed() -> None:
    steps = ["а) 40 + 35 = 76", "б) 90 - 18 = 72", "в) 12 * 3 = 35"]
    line, _ = task_line(0, checked(steps, condition="Вычисли: а) 40 + 35; б) 90 - 18; в) 12 * 3"))
    assert line == "№2.181 — есть ошибки в пунктах а), в) ❌ Верно: б)."


def test_actions_under_an_item_belong_to_it() -> None:
    steps = ["а) 300 - 264 = 36", "16452 : 36 = 456", "39452 - 456 = 38996", "б) 90 - 18 = 72"]
    line, _ = task_line(0, checked(steps))
    assert line == "№2.181 — есть ошибка в пункте а) ❌ Верно: б)."


def test_blank_line_does_not_spoil_an_item() -> None:
    line, _ = task_line(0, checked(["а) 5 + 3 = 8", "", "б) 4 + 4 = 9"]))
    assert line == "№2.181 — есть ошибка в пункте б) ❌ Верно: а)."


def test_numbered_actions_inside_lettered_items_are_not_items() -> None:
    steps = ["а) 1) 300 - 264 = 36", "2) 16452 : 36 = 456", "б) 90 - 18 = 72"]
    line, _ = task_line(0, checked(steps))
    assert line == "№2.181 — есть ошибка в пункте а) ❌ Верно: б)."


def test_error_before_the_first_item_is_not_hidden() -> None:
    line, _ = task_line(0, checked(["5 + 5 = 11", "а) 5 + 3 = 9", "б) 4 + 4 = 8"]))
    assert line == "№2.181 — есть ошибка в строке «5 + 5 = 11» ❌"


def test_error_without_items_quotes_the_line() -> None:
    line, _ = task_line(0, checked(["700 - 400 = 310"], number=7, label="7"))
    assert line == "№7 — есть ошибка в строке «700 − 400 = 310» ❌"
    long = "329503 + 12146 * 28 + 715 * 449 = 990000"
    line, _ = task_line(0, checked([long], number=7, label="7"))
    assert line == "№7 — есть ошибка в строке «329503 + 12146 · 28 + 715 · 44…» ❌"


def test_uncertain_line_lists_items_too() -> None:
    line, _ = task_line(0, checked(STEPS_2181, condition=CONDITION_2181, label=None))
    assert line == (
        "Задание 2181 — не уверен, что верно прочитал запись 🤔 "
        "Под вопросом: а), б). Верно: в). Не смог проверить: г)."
    )


def test_only_condition_without_solution() -> None:
    steps = ["а) (24 - 16) + (201 + 14)", "в) (m + 41) + (n - 17)", "б) (x + 86) + 109"]
    line, _ = task_line(0, checked(steps, number=2183, label="2.183"))
    assert line == "№2.183 — вижу только условие, решения пока нет 🤔"
    answers_only = checked(["а) 75", "б) 72"], number=5, label="5")
    assert task_line(0, answers_only)[0] == "№5 — не смог разобрать решение 🤔"


# --- заголовок и «что дальше» ---


def test_header_counts_only_checked_tasks() -> None:
    wrong = checked(ITEMS_2181, condition=CONDITION_2181)
    unsure = checked(["а) (24 - 16) + (201 + 14)"], number=2183, label="2.183")
    right = checked(["2 + 2 = 4"], number=4, label="4")
    assert review_header(ChatState(tasks=[wrong, unsure])) == "Проверил! 0 из 1 верно.\n"
    assert review_header(ChatState(tasks=[unsure])) == "Проверил!\n"
    assert review_header(ChatState(tasks=[right, wrong])) == "Проверил! 1 из 2 верно.\n"


def test_summary_without_errors_says_what_next() -> None:
    right = checked(["2 + 2 = 4"], number=4, label="4")
    text, buttons = review_message(ChatState(tasks=[right]))
    assert text.endswith(f"\n{NEXT_PHOTO}") and buttons == []
    wrong = checked(["2 + 2 = 5"], number=5, label="5")
    text, buttons = review_message(ChatState(tasks=[right, wrong]))
    assert NEXT_PHOTO not in text and buttons  # сначала разбор ошибки
    asked = ChatState(
        tasks=[checked(STEPS_2181, condition=CONDITION_2181)],
        clarifications=[Clarification(task_index=0, kind="result", line_index=0)],
    )
    assert NEXT_PHOTO not in review_message(asked)[0]  # сначала вопрос
