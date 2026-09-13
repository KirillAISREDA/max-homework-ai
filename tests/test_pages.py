"""Роли страниц и сопоставление «учебник + тетрадь» (сессия 9, живой тест):
альбом из фото учебника и тетради; условие №19 берётся из учебника."""

from hwcheck.bot.pages import (
    attach_conditions,
    describe_tasks,
    format_numbers,
    mark_written_numbers,
    merge_textbook,
    page_role,
    task_label,
    written_numbers,
)
from hwcheck.pipeline.schemas import VisionPage, VisionTask


def _task(
    number: int,
    text: str = "",
    steps: list[str] | None = None,
    answer: str | None = None,
    *,
    on_page: bool = True,
) -> VisionTask:
    return VisionTask(
        number=number,
        task_text=text,
        student_solution_steps=steps or [],
        student_answer=answer,
        confidence=0.9,
        number_on_page=on_page,
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


def test_notebook_with_continuation_lines_is_notebook() -> None:
    # живые логи 08.09: тетрадь «Стр. 5 № 4» определилась как учебник, бот ответил
    # «Вижу страницу учебника»
    steps = [
        "(1/2 + 1/3)*(-12)",
        "= (-12)/2 + (-12)/3 = -6 + (-4) = -10",
        "(1/3 - 1/4)*(-24)",
        "= (-24)/3 - (-24)/4 = -8 - (-6) = -2",
    ]
    assert page_role(_page([_task(4, "(1/2 + 1/3)*(-12)", steps)])) == "notebook"


class TestSyntheticNumbers:
    """Живые логи 13.09: структуризатор нумерует задания без номера с 1, и совпадение
    «номеров» склеивало деление дробей из тетради с «Отметьте точки K, L и M»."""

    def test_task_numbers_are_read_from_transcript(self) -> None:
        transcript = (
            "Д/з № 13\nN 462 vcevee.ru\nN° 35 (2)\n23. Вычисли и сделай проверку\n"
            "Стр. 5 № 4\n1.124 Выполните действия\nI способ\n1) 312\n"
            "4/5 : 9/10 = 4/5 * 10/9 = 40/45 = 8/9\nЗадание 7\n"
        )
        assert written_numbers(transcript) == {13, 462, 35, 23, 4, 7}

    def test_numbers_missing_from_transcript_are_marked_synthetic(self) -> None:
        page = _page([_task(1, "", ["4/5 : 9/10 = 8/9"]), _task(23, "Вычисли")])
        marked = mark_written_numbers(page, "4/5 : 9/10 = 8/9\n23. Вычисли")
        assert [t.number_on_page for t in marked.tasks] == [False, True]

    def test_synthetic_numbers_do_not_attach_an_unrelated_condition(self) -> None:
        notebook = [_task(1, "", ["4/5 : 9/10 = 4/5 * 10/9 = 40/45 = 8/9"], on_page=False)]
        textbook = [_task(1, "Отметьте точки K, L и M, лежащие на луче FE", on_page=False)]
        assert attach_conditions(notebook, textbook) == notebook

    def test_number_written_on_one_side_only_is_not_a_match(self) -> None:
        notebook = [_task(2, "", ["9/10 : 4/5 = 9/8"])]
        textbook = [_task(2, "Проведите прямую SR", on_page=False)]
        assert attach_conditions(notebook, textbook) == notebook

    def test_content_match_still_works_for_synthetic_numbers(self) -> None:
        notebook = [_task(1, "", ["700 - (220 + 180) = 300"], "300", on_page=False)]
        merged = attach_conditions(notebook, TEXTBOOK)
        assert merged[0].number == 19

    def test_synthetic_textbook_numbers_do_not_overwrite_known_conditions(self) -> None:
        # живые логи 13.09: страница «1.124» (без номеров, пронумерована с 1) затёрла
        # запомненное условие «Отметьте точки» — у него тоже был придуманный №1
        known = merge_textbook([], [_task(1, "Отметьте точки K, L и M", on_page=False)])
        new = [
            _task(1, "Выполните действия 1.124", on_page=False),
            _task(2, "Найдите значение выражения", on_page=False),
        ]
        texts = [t.task_text for t in merge_textbook(known, new)]
        assert sorted(texts) == [
            "Выполните действия 1.124",
            "Найдите значение выражения",
            "Отметьте точки K, L и M",
        ]

    def test_written_number_replaces_only_written_condition(self) -> None:
        known = merge_textbook([], [_task(19, "старое"), _task(1, "без номера", on_page=False)])
        merged = merge_textbook(known, [_task(19, "новое"), _task(1, "напечатан №1")])
        assert sorted((t.number, t.number_on_page, t.task_text) for t in merged) == [
            (1, False, "без номера"),
            (1, True, "напечатан №1"),
            (19, True, "новое"),
        ]

    def test_same_synthetic_condition_is_not_duplicated(self) -> None:
        page = [_task(1, "Отметьте точки", on_page=False)]
        assert len(merge_textbook(merge_textbook([], page), page)) == 1

    def test_content_match_picks_synthetic_condition_among_equal_numbers(self) -> None:
        textbook = [
            _task(1, "Отметьте точки K и M", on_page=True),
            _task(1, "Денис бежал 10 мин со скоростью 110 м/мин", on_page=False),
        ]
        notebook = [_task(3, "", ["110 * 10 = 1100"], on_page=False)]
        merged = attach_conditions(notebook, textbook)
        assert merged[0].task_text.startswith("Денис")
        # придуманный номер учебника ничего не говорит ребёнку — остаётся номер тетради
        assert (merged[0].number, merged[0].number_on_page) == (3, False)

    def test_attached_condition_brings_its_number_origin(self) -> None:
        # придуманный №1 тетради совпал по числам с напечатанным №19 — это уже «№19»
        notebook = [_task(1, "", ["700 - (220 + 180) = 300"], on_page=False)]
        merged = attach_conditions(notebook, [TEXTBOOK[3]])
        assert (merged[0].number, merged[0].number_on_page) == (19, True)


def test_task_label_hides_synthetic_number_sign() -> None:
    assert task_label(_task(19)) == "№19"
    assert task_label(_task(1, on_page=False)) == "Задание 1"


def test_describe_tasks() -> None:
    assert describe_tasks([_task(16), _task(17), _task(18)]) == "№16–18"
    assert describe_tasks([_task(1, on_page=False), _task(2, on_page=False)]) == "2 задания"
    assert describe_tasks([_task(5), _task(1, on_page=False)]) == "№5 и ещё 1 задание"
    assert describe_tasks([_task(n, on_page=False) for n in range(1, 6)]) == "5 заданий"
    assert describe_tasks([_task(n, on_page=False) for n in range(1, 12)]) == "11 заданий"
    assert describe_tasks([_task(n, on_page=False) for n in range(1, 23)]) == "22 задания"
