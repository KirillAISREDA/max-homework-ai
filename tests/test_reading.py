"""Ошибка чтения — не ошибка ребёнка (спецификация 2026-09-30-math-misread-guards-design.md).

Живая проверка 30.09, №2.181: распознавание потеряло цифры в пункте а), прочитало «1662372» как
«16623 * 42» в пункте б) и «601» как «667» в пункте в). Пересчёт строк выдал две «ошибки» с целью
разбора «3175254/61», а настоящую ошибку пункта а) пропустил. Печатное условие было в руках.
"""

import time

import pytest

from hwcheck.bot.check import validator_only_grade
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.reading import printed_items, review_reading
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import check_steps
from hwcheck.subjects.math.module import findings_from_grade

CONDITION_2181 = (
    "Выполните действия:\n"
    "а) 39 452 - 16 452 : (300 - 264);\n"
    "в) 93 601 - 601 * (231 - 88);\n"
    "б) 2 558 304 : 63 + 1 662 372 : 61;\n"
    "г) 329 503 + 12 146 * 28 + 7154."
)
STEPS_2181 = [
    "a) 9454 - 16452 : (300 - 264) = 8997",
    "б) 2558304 : 63 + 16623 * 42 : 61 = 27252",
    "в) 93601 - 667 * (231 - 88) = 7658",
    "г) 329503 + 12446 * 28 + 715 - 449 =",
]
CONDITION = "Вычисли: а) 40 + 35; б) 90 - 18"


# --- пункты печатного условия ---


def test_printed_items_of_live_condition() -> None:
    items = printed_items(CONDITION_2181)
    assert {label: str(item.value) for label, item in items.items()} == {
        "а": "38995",
        "в": "7658",
        "б": "67860",
        "г": "676745",
    }
    assert items["а"].expression == "39452 - 16452 : (300 - 264)"
    assert items["а"].operators == 3


def test_numbered_items_and_brackets_ending_with_a_number() -> None:
    condition = "Вычислите:\n1) 1058 : (5244 : 19 : 12);\n2) 20 748 : 57 : (182 : 13)."
    items = printed_items(condition)
    assert {label: str(item.value) for label, item in items.items()} == {"1": "46", "2": "26"}


@pytest.mark.parametrize(
    ("condition", "labels"),
    [
        ("Запишите разность: а) 27 * 3 и 38 - 19; б) 168 : 4", {"б"}),  # «и» — не выражение
        ("Упростите: а) (m + 41) + (n - 17); б) (x + 86) + 109", set()),  # буквенные выражения
        ("Проверьте: а) 25 * 4 = 100; б) 3 + 2", {"б"}),  # равенство сверять не с чем
        ("а) 125; б) 5 + 3", {"б"}),  # число без действия
        ("а) 5 + 3; а) 6 + 1", set()),  # метка дважды — какой пункт имел в виду ребёнок, неясно
        ("Купили 30 кг краски (см. рис. а) и 7 кг лака", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_items_unfit_for_comparison_are_dropped(condition: str | None, labels: set[str]) -> None:
    assert set(printed_items(condition)) == labels


def test_many_labels_are_parsed_in_linear_time() -> None:
    # ревью: глубина скобок пересчитывалась от начала текста для каждой метки
    started = time.perf_counter()
    assert printed_items("а) 1 " * 3000) == {}
    assert time.perf_counter() - started < 0.5


def test_mixed_number_is_not_glued_as_thousands() -> None:
    items = printed_items("а) 8 3/7 - 4 4/7; б) 1 200 + 300")
    assert {label: str(item.value) for label, item in items.items()} == {"а": "27/7", "б": "1500"}


# --- сверка строки с печатным пунктом ---


def test_live_case_is_uncertain_not_a_false_error() -> None:
    result = validator_only_grade(STEPS_2181, condition=CONDITION_2181)
    assert (result.verdict, result.uncertain_reason) == ("uncertain", "line_misread")
    assert result.first_error_line is None
    assert [c.status for c in result.line_checks] == ["skipped", "skipped", "ok", "skipped"]
    assert [c.misread for c in result.line_checks] == [True, True, False, False]
    [finding] = findings_from_grade(0, result)
    assert (finding.strength, finding.detail) == (
        "candidate",
        "не уверен, что верно прочитал запись",
    )


def test_live_case_with_verified_reference_is_the_same() -> None:
    ref = RefSolution(steps=["39452 - 457 = 38995"], answer="38995")
    result = grade(STEPS_2181, None, ref, condition=CONDITION_2181)
    assert (result.verdict, result.uncertain_reason) == ("uncertain", "line_misread")


def test_written_result_equal_to_printed_value_is_correct_whatever_the_left_part() -> None:
    result = validator_only_grade(["а) 48 + 35 = 75", "б) 90 - 18 = 72"], condition=CONDITION)
    assert result.verdict == "correct"
    assert result.line_checks[0].values == ["75", "75"]


@pytest.mark.parametrize(
    "line",
    [
        "а) 2 * 3 + 4 = 5 + 4 = 10",
        "а) 2 * 3 + 4 = 6 + 5 = 10",
    ],
)
def test_slip_inside_chain_is_not_hidden_by_right_result(line: str) -> None:
    # ревью: верный итог не отменяет ошибку в середине цепочки
    result = validator_only_grade([line, "б) 7 * 8 = 56"], condition="а) 2 * 3 + 4; б) 7 * 8")
    assert (result.verdict, result.first_error_line) == ("wrong", 1)


def test_chain_with_misread_start_and_right_steps_is_correct() -> None:
    line = "а) 2 * 8 + 4 = 6 + 4 = 10"  # «3» прочиталась как «8», дальше всё сходится
    result = validator_only_grade([line, "б) 7 * 8 = 56"], condition="а) 2 * 3 + 4; б) 7 * 8")
    assert result.verdict == "correct"


def test_wrong_result_of_correctly_copied_item_is_an_error_with_printed_target() -> None:
    steps = ["а) 39452 - 16452 : (300 - 264) = 38997"]
    result = validator_only_grade(steps, condition=CONDITION_2181)
    assert (result.verdict, result.first_error_line) == ("wrong", 1)
    [finding] = findings_from_grade(0, result)
    assert (finding.strength, finding.expected) == ("verified", "38995")


def test_self_consistent_line_that_differs_from_printed_item_is_uncertain() -> None:
    # ребёнок переписал 39454 вместо 39452 и свой пример посчитал верно — или цифру потеряло
    # распознавание: по одной строке не различить, ошибку не утверждаем
    steps = ["а) 39454 - 16452 : (300 - 264) = 38997"]
    result = validator_only_grade(steps, condition=CONDITION_2181)
    assert (result.verdict, result.uncertain_reason) == ("uncertain", "line_misread")


def test_single_action_under_item_label_is_checked_as_before() -> None:
    steps = ["а) 300 - 264 = 36", "16452 : 36 = 457", "39452 - 457 = 38995"]
    assert validator_only_grade(steps, condition=CONDITION_2181).verdict == "correct"
    wrong = ["а) 300 - 264 = 46", *steps[1:]]
    result = validator_only_grade(wrong, condition=CONDITION_2181)
    assert (result.verdict, result.first_error_line) == ("wrong", 1)


def test_real_error_wins_over_misread_line() -> None:
    result = validator_only_grade(["а) 48 + 35 = 83", "б) 90 - 18 = 82"], condition=CONDITION)
    assert (result.verdict, result.first_error_line) == ("wrong", 2)
    assert result.line_checks[0].misread


# --- невозможное «верное значение» ---


@pytest.mark.parametrize(
    "line",
    [
        "2558304 : 63 + 16623 * 42 : 61 = 27252",  # верное 3175254/61
        "93601 - 667 * (231 - 88) = 7658",  # верное −1780
        "10 : 3 = 3",  # деление с остатком, а не ошибка
    ],
)
def test_impossible_expected_value_is_uncertain(line: str) -> None:
    result = validator_only_grade([line])
    assert (result.verdict, result.uncertain_reason) == ("uncertain", "line_misread")
    assert result.line_checks[0].misread


@pytest.mark.parametrize(
    "line",
    [
        "803 + 169 = 753",
        "12 - 20 = 8",  # ошибка знака: верное −8
        "12 - 20 = -9",  # ребёнок сам пишет отрицательные числа
        "(-12) : 2 + 3 = 5",
        "7 : 2 = 3",  # верное 3,5 — обычное школьное значение
        "1/3 + 1/3 = 1/3",  # дроби записаны в самой строке
        "0,5 : 3 = 0,2",
    ],
)
def test_ordinary_errors_stay_errors(line: str) -> None:
    result = validator_only_grade([line])
    assert (result.verdict, result.first_error_line) == ("wrong", 1)


def test_equation_with_fractional_root_is_not_touched() -> None:
    steps = ["3x = 7", "x = 5"]
    before = check_steps(steps)
    assert [c.status for c in before] == ["ok", "mismatch"]
    assert review_reading(before, None) == before


def test_validator_itself_is_not_relaxed() -> None:
    # тем же валидатором проверяется эталон солвера: «10 : 3 = 3» в эталоне — брак, а не сомнение
    [check] = check_steps(["10 : 3 = 3"])
    assert (check.status, check.misread) == ("mismatch", False)


def test_review_is_idempotent() -> None:
    once = review_reading(check_steps(STEPS_2181, condition=CONDITION_2181), CONDITION_2181)
    assert review_reading(once, CONDITION_2181) == once
