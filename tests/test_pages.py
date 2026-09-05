"""Роли страниц и сопоставление «учебник + тетрадь» (сессия 9, живой тест):
альбом из фото учебника и тетради; условие №19 берётся из учебника."""

from hwcheck.bot.pages import attach_conditions, format_numbers, merge_textbook, page_role
from hwcheck.pipeline.schemas import VisionPage, VisionTask


def _task(
    number: int,
    text: str = "",
    steps: list[str] | None = None,
    answer: str | None = None,
) -> VisionTask:
    return VisionTask(
        number=number,
        task_text=text,
        student_solution_steps=steps or [],
        student_answer=answer,
        confidence=0.9,
    )


def _page(tasks: list[VisionTask]) -> VisionPage:
    return VisionPage(tasks=tasks, page_ok=True)


TEXTBOOK = [
    _task(16, "Объясни, что обозначают записи", ["12 + x = 12", "x + 24 = 24"]),
    _task(17, "Вычисли и выполни проверку", ["803 + 169", "425 + 375"]),
    _task(18, "Садовод заготовил 250 г семян астр и 240 г семян гвоздик"),
    _task(
        19,
        "В загородном лагере за 3 летних месяца отдохнуло 700 ребят. В июне — 220, в июле — 180.",
    ),
    _task(20, "Реши уравнения", ["180 - x = 100"]),
    _task(21, "", ["15 * 10 + (30 - 20) * 5"]),
    _task(22, "Переставь карточки с цифрами"),
]
NOTEBOOK_19 = [
    _task(
        19,
        "Июнь — 220 чел. Июль — 180 чел. Август — ? Всего — 700 чел.",
        ["700 - (220 + 180) = 300"],
        "300",
    )
]


def test_textbook_page_is_textbook_even_with_expression_lines() -> None:
    assert page_role(_page(TEXTBOOK)) == "textbook"


def test_notebook_page_with_answer_is_notebook() -> None:
    assert page_role(_page(NOTEBOOK_19)) == "notebook"


def test_notebook_page_with_bare_columns_is_notebook() -> None:
    page = _page([_task(1, "", ["999 + 1 = 1000", "900 - 1 = 899"])])
    assert page_role(page) == "notebook"


def test_empty_page_is_empty() -> None:
    assert page_role(None) == "empty"
    assert page_role(_page([])) == "empty"
    assert page_role(_page([_task(1)])) == "empty"


def test_merge_textbook_overrides_by_number_and_sorts() -> None:
    known = [_task(19, "старое условие"), _task(3, "три")]
    merged = merge_textbook(known, [_task(19, "новое условие"), _task(20, "двадцать"), _task(21)])
    assert [t.number for t in merged] == [3, 19, 20]
    assert merged[1].task_text == "новое условие"


def test_attach_conditions_prefers_textbook_over_notebook_notes() -> None:
    merged = attach_conditions(NOTEBOOK_19, TEXTBOOK)
    assert merged[0].task_text.startswith("В загородном лагере")
    assert merged[0].student_solution_steps == ["700 - (220 + 180) = 300"]
    assert merged[0].student_answer == "300"


def test_attach_conditions_keeps_unmatched_task_as_is() -> None:
    notebook = [_task(7, "", ["2 + 2 = 4"])]
    assert attach_conditions(notebook, TEXTBOOK) == notebook


def test_format_numbers() -> None:
    assert format_numbers([16, 17, 18, 19, 20, 21, 22]) == "№16–22"
    assert format_numbers([3, 7]) == "№3, №7"
    assert format_numbers([5]) == "№5"
    assert format_numbers([1, 2, 3, 7]) == "№1, №2, №3, №7"


def test_attach_by_content_when_number_misread() -> None:
    # рукописное «№19» прочитано как 29; краткая запись содержит числа условия №19
    notebook = [
        _task(29, "", ["Июнь - 220 чел.", "Июль - 180 чел.", "700 - (220 + 180) = 300"], "300")
    ]
    merged = attach_conditions(notebook, TEXTBOOK)
    assert merged[0].number == 19
    assert merged[0].task_text.startswith("В загородном лагере")


def test_exact_number_wins_a_tie_with_content() -> None:
    textbook = [_task(5, "Найди сумму 220 и 180"), _task(7, "Вычисли 220 + 180 + 1")]
    notebook = [_task(7, "", ["220 + 180 = 400"])]
    assert attach_conditions(notebook, textbook)[0].task_text == "Вычисли 220 + 180 + 1"


def test_no_shared_numbers_means_no_content_match() -> None:
    notebook = [_task(29, "", ["2 + 2 = 4"])]
    assert attach_conditions(notebook, TEXTBOOK) == notebook


def test_textbook_with_expression_lines_in_every_task_is_still_textbook() -> None:
    # структуризатор может сложить печатные выражения в строки у КАЖДОГО задания:
    # без вычисленного равенства (число = число) и без «Ответ:» это всё ещё учебник
    page = _page(
        [
            _task(16, "Объясни записи", ["12 + x = 12", "x + 24 = 24"]),
            _task(17, "Вычисли", ["803 + 169", "425 + 375"]),
            _task(18, "Садовод заготовил 250 г", ["250 : 5", "240 : 8"]),
            _task(19, "В лагере отдохнуло 700 ребят", ["700 ребят"]),
            _task(20, "Реши уравнения", ["180 - x = 100"]),
            _task(21, "Вычисли", ["15 * 10 + (30 - 20) * 5"]),
            _task(22, "Переставь карточки", ["7 3 - 2 5 = 5 8"]),
        ]
    )
    assert page_role(page) == "textbook"


def test_computed_equality_without_answer_is_notebook() -> None:
    page = _page([_task(19, "Всего 700 чел., в июне 220, в июле 180", ["700 - (220 + 180) = 300"])])
    assert page_role(page) == "notebook"


def test_pure_work_page_without_conditions_is_notebook() -> None:
    page = _page([_task(20, "", ["x + 24 = 24", "x = 24 - 24", "x = 0"])])
    assert page_role(page) == "notebook"


def test_small_shared_numbers_do_not_force_a_wrong_match() -> None:
    # «2» и «10» встречаются в половине задач начальной школы — это не подпись задачи
    textbook = [_task(3, "У Маши было 2 яблока, она купила ещё 10. Сколько стало?")]
    notebook = [_task(29, "", ["2 * 10 = 20", "20 : 4 = 5"], "5")]
    assert attach_conditions(notebook, textbook) == notebook


def test_numbers_shared_by_several_conditions_are_not_distinctive() -> None:
    textbook = [_task(5, "Найди сумму 220 и 180"), _task(6, "Найди разность 220 и 180")]
    notebook = [_task(29, "", ["220 + 180 = 400"], "400")]
    assert attach_conditions(notebook, textbook) == notebook


def test_two_notebook_tasks_are_not_attached_to_the_same_condition() -> None:
    notebook = [
        _task(19, "", ["700 - (220 + 180) = 300"], "300"),
        _task(20, "", ["220 + 180 = 400"], "400"),
    ]
    merged = attach_conditions(notebook, [TEXTBOOK[3]])  # на странице только №19
    assert merged[0].number == 19
    assert merged[1] == notebook[1]


def test_decimal_separator_does_not_matter_for_matching() -> None:
    textbook = [_task(5, "Вычисли 4.5 + 12.25")]
    notebook = [_task(29, "", ["4,5 + 12,25 = 16,75"], "16,75")]
    assert attach_conditions(notebook, textbook)[0].number == 5


def test_one_stray_computed_line_does_not_flip_a_textbook_page() -> None:
    # 7 условий и одна случайная строка «число = число» — это всё ещё учебник
    tasks = [_task(n, f"Условие {n}") for n in range(16, 23)]
    tasks[2] = _task(18, "Условие 18", ["250 : 5 = 50"])
    assert page_role(_page(tasks)) == "textbook"


def test_half_of_tasks_with_work_is_a_notebook() -> None:
    # ученик переписал краткие условия и решил четыре задания из семи
    tasks = [_task(n, f"Краткая запись {n}") for n in range(16, 23)]
    for i in range(4):
        tasks[i] = _task(16 + i, f"Краткая запись {16 + i}", ["2 + 2 = 4"])
    assert page_role(_page(tasks)) == "notebook"
