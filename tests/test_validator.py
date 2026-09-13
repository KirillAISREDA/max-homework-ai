from hwcheck.pipeline.validator import check_steps, compare_answers


class TestCheckSteps:
    def test_all_correct(self) -> None:
        checks = check_steps(["2+2=4", "4*3=12"])
        assert [c.status for c in checks] == ["ok", "ok"]

    def test_arithmetic_mismatch_detected(self) -> None:
        checks = check_steps(["2+2=5"])
        assert checks[0].status == "mismatch"
        assert checks[0].values == ["4", "5"]

    def test_mixed_numbers_ok(self) -> None:
        checks = check_steps(["8 3/7 - 4 4/7 = 3 6/7"])
        assert checks[0].status == "ok"

    def test_student_error_in_fraction(self) -> None:
        # реальная ошибка: 7 1/8 + 2 5/8 = 9 6/8, ученик написал 9 5/8
        checks = check_steps(["7 1/8 + 2 5/8 = 9 5/8"])
        assert checks[0].status == "mismatch"

    def test_text_line_skipped(self) -> None:
        checks = check_steps(["Ответ: 90 км/ч"])
        assert checks[0].status == "skipped"

    def test_chain_equality_mismatch_in_middle(self) -> None:
        checks = check_steps(["5+5=11=2*5"])
        assert checks[0].status == "mismatch"

    def test_decimal_comma(self) -> None:
        checks = check_steps(["4,7+0,3=5,0"])
        assert checks[0].status == "ok"


class TestCompareAnswers:
    def test_equal_numbers(self) -> None:
        assert compare_answers("90", "90") is True

    def test_units_stripped_from_student_answer(self) -> None:
        assert compare_answers("90 км/ч", "90") is True

    def test_equivalent_fraction_forms(self) -> None:
        assert compare_answers("3 6/7", "27/7") is True

    def test_decimal_comma_vs_dot(self) -> None:
        assert compare_answers("4,5", "4.5") is True

    def test_different_answers(self) -> None:
        assert compare_answers("86", "90") is False

    def test_unparseable_returns_none(self) -> None:
        assert compare_answers("не знаю", "90") is None

    def test_empty_returns_none(self) -> None:
        assert compare_answers("", "90") is None


class TestContinuationLines:
    """Живые логи 08.09: запись переносится на строку «= …», и такие строки не
    пересчитывались — тетрадь с ними определялась как страница учебника."""

    def test_line_starting_with_equals_continues_previous_expression(self) -> None:
        checks = check_steps(["(1/2 + 1/3)*(-12)", "= (-12)/2 + (-12)/3 = -6 + (-4) = -10"])
        assert [c.status for c in checks] == ["skipped", "ok"]

    def test_continuation_of_an_equality_checks_against_its_result(self) -> None:
        checks = check_steps(["15 * 10 + 5 = 150 + 5", "= 156"])
        assert [c.status for c in checks] == ["ok", "mismatch"]
        assert checks[1].line == "= 156"

    def test_text_line_with_equals_is_not_continued(self) -> None:
        # «Дано: a=5, b=3» — не выражение; хвост «3» не должен стать левой частью
        checks = check_steps(["Дано: a=5, b=3", "= 5 - 3 = 2"])
        assert [c.status for c in checks] == ["skipped", "skipped"]

    def test_ok_line_keeps_computed_values(self) -> None:
        assert check_steps(["16 * 10 = 160"])[0].values == ["160", "160"]
