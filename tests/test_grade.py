from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.solver import RefSolution

REF = RefSolution(steps=["430/5 = 86"], answer="86", units="км/ч")


def test_correct_answer() -> None:
    result = grade(["430:5=86"], "86 км/ч", REF)
    assert result.verdict == "correct"
    assert result.answers_match is True
    assert result.first_error_line is None


def test_wrong_answer_with_arithmetic_error() -> None:
    result = grade(["430:5=96"], "96", REF)
    assert result.verdict == "wrong"
    assert result.answers_match is False
    assert result.first_error_line == 1


def test_wrong_answer_but_steps_look_consistent() -> None:
    # ученик решал не то (стратегическая ошибка): арифметика верна, ответ не тот
    result = grade(["430-5=425"], "425", REF)
    assert result.verdict == "wrong"
    assert result.first_error_line is None


def test_correct_answer_with_slip_in_steps() -> None:
    # ответ верный, но в промежуточном шаге описка
    result = grade(["430:5=87", "Ответ: 86"], "86", REF)
    assert result.verdict == "correct"
    assert result.slip_lines == [1]


def test_unparseable_answer_is_uncertain() -> None:
    result = grade(["<неразборчиво>"], "<неразборчиво>", REF)
    assert result.verdict == "uncertain"
    assert result.answers_match is None


def test_arithmetic_error_without_written_answer_is_wrong() -> None:
    # живой тест (сессия 9): «803 + 169 = 753» без строки «Ответ» — валидатор доказал
    # ошибку (972), вердикт обязан быть wrong, а не uncertain
    ref = RefSolution(steps=["803 + 169 = 972", "425 - 375 = 50"], answer="[972, 50]", units=None)
    result = grade(["803 + 169 = 753"], None, ref)
    assert result.verdict == "wrong"
    assert result.first_error_line == 1


def test_unparseable_answer_with_consistent_steps_stays_uncertain() -> None:
    ref = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300", units=None)
    result = grade(["220 + 180 = 400", "700 - 400 = 300"], None, ref)
    assert result.verdict == "uncertain"
