"""Деление уголком, которое распознавание не читает (живой альбом 14.09, №55).

Распознавание рвёт уголок на обрывки («− 6», «14», «− 14», «8») и склеивает мусорные равенства
(«748 * 374 = 279352»); валидатор принимает «14 / − 14 / 8» за столбик «14 − 14 = 8». Это не
ошибки ребёнка: ошибка — только строка, переписавшая пример из условия с неверным результатом,
«верно» — если результаты всех примеров условия нашлись в работе, иначе «не уверен».
"""

from hwcheck.bot.check import validator_only_grade
from hwcheck.pipeline.grade import condition_examples, grade, is_long_division
from hwcheck.pipeline.solver import RefSolution

CONDITION = "Выполни деление и проверку.\n748 : 2   987 : 3   648 : 4   756 : 6"

# транскрипция №55 из повторного прогона живого альбома 14.09: у ребёнка всё верно
LIVE_55 = [
    "748 * 374 = 279352",
    "- 748",
    "- 6",
    "- 74",
    "- 8",
    "* 374   9873 : 329 = 30",
    "  748     - 9873",
    "  748      987",
    "        - 329",
    "         329",
    "           0",
    "* 39   - 64842   * 762   - 7569",
    "  987     24       648     726",
    "  987     - 24      56       36",
    "          0              - 36",
    "                              0",
    "* 126",
    "  756",
]

# тот же уголок без искажений: результаты 374, 329, 162, 126 и проверки умножением
CLEAN_55 = [
    *["748 | 2", "- 6", "374", "14", "- 14", "8", "- 8", "0", "* 374", "2", "748"],
    *["987 | 3", "- 9", "329", "8", "- 6", "27", "- 27", "0"],
    *["648 | 4", "- 4", "162", "24", "- 24", "8", "- 8", "0"],
    *["756 | 6", "- 6", "126", "15", "- 12", "36", "- 36", "0"],
]


def test_condition_examples_are_listed_expressions_after_text() -> None:
    assert condition_examples(CONDITION) == ["748 : 2", "987 : 3", "648 : 4", "756 : 6"]
    assert condition_examples("Масса ящика 12 кг, а пустого в 6 раз меньше?") == []
    assert condition_examples(None) == []


def test_long_division_needs_division_and_fragments() -> None:
    assert is_long_division(LIVE_55, CONDITION)
    assert is_long_division(CLEAN_55, None)
    # простой столбик сложения — не уголок: его ошибки валидатор ловит как раньше
    assert not is_long_division(["803", "+ 169", "-----", "753"], None)
    assert not is_long_division(["20 : 4 = 5", "20 + 5 + 10 = 35"], None)


def test_garbled_long_division_is_uncertain_not_wrong() -> None:
    result = validator_only_grade(LIVE_55, condition=CONDITION)
    assert result.verdict == "uncertain"
    assert result.uncertain_reason == "column_unreadable"
    assert result.first_error_line is None


def test_long_division_with_all_results_found_is_correct() -> None:
    result = validator_only_grade(CLEAN_55, condition=CONDITION)
    assert result.verdict == "correct"


def test_rewritten_example_with_wrong_result_is_an_error() -> None:
    steps = ["748 : 2 = 375", "- 6", "14", "- 14", "8", "- 8", "0"]
    result = validator_only_grade(steps, condition=CONDITION)
    assert result.verdict == "wrong"
    assert result.first_error_line == 1


def test_missing_result_is_uncertain() -> None:
    without_last = CLEAN_55[: CLEAN_55.index("756 | 6")]
    result = validator_only_grade(without_last, condition=CONDITION)
    assert result.verdict == "uncertain"
    assert result.uncertain_reason == "column_unreadable"


def test_result_equal_to_condition_number_is_not_evidence() -> None:
    """«84 : 42» = 2, а 2 уже есть в условии («748 : 2») — не отличить результат от условия."""
    condition = "Вычисли.\n748 : 2   84 : 42"
    steps = ["748 | 2", "- 6", "374", "14", "- 14", "8", "- 8", "0", "84 | 42", "- 84"]
    assert validator_only_grade(steps, condition=condition).verdict == "uncertain"


def test_without_condition_garbled_division_is_uncertain() -> None:
    result = validator_only_grade(LIVE_55)
    assert result.verdict == "uncertain"
    assert result.uncertain_reason == "column_unreadable"


def test_single_example_with_reference_uses_answer() -> None:
    condition = "Выполни деление: 756 : 6"
    steps = ["756 | 6", "- 6", "15", "- 12", "36", "- 36", "0"]
    ref = RefSolution(steps=["756 : 6 = 126"], answer="126")
    assert grade(steps, "126", ref, condition=condition).verdict == "correct"
    assert grade(steps, "127", ref, condition=condition).verdict == "wrong"
    # ответа нет, результат в записи не нашёлся — «не уверен», а не «ошибка» по обрывкам
    no_answer = grade(steps, None, ref, condition=condition)
    assert no_answer.verdict == "uncertain"
    assert no_answer.uncertain_reason == "column_unreadable"


def test_simple_column_error_is_still_wrong() -> None:
    assert validator_only_grade(["803", "+ 169", "-----", "753"]).verdict == "wrong"


# --- ревью ---


def test_wrong_multiplication_check_is_an_error() -> None:
    """Ревью: «проверку» ребёнка (частное · делитель) нельзя пропускать как обрывок."""
    condition = "Выполни деление и проверку: 748 : 2"
    steps = ["748 : 2 = 374", "- 6", "14", "- 14", "8", "- 8", "0", "374 * 2 = 700"]
    result = validator_only_grade(steps, condition=condition)
    assert result.verdict == "wrong"
    assert result.first_error_line == 8


def test_clock_time_in_condition_is_not_division() -> None:
    """Ревью: «в 10:45» в тексте задачи — не пример на деление."""
    steps = ["20 + 15 = 35", "12", "8", "5"]
    assert not is_long_division(steps, "Автобус ушёл в 10:45.")
    assert is_long_division(["756 | 6", "- 6", "15", "- 12"], None)
