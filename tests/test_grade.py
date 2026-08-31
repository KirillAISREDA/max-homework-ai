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
