"""Ответ и результат шага, записанные по-школьному (сессия 9, живой тест):
«Ответ: 300 человек отдыхали в августе», «= 300 (чел.) - отдохну-». Число
извлекается из начала, но только когда хвост не содержит других чисел —
«2 км 300 м» по-прежнему не парсится (uncertain, а не ложная ошибка)."""

from hwcheck.pipeline.validator import check_steps, compare_answers


class TestAnswerSentence:
    def test_answer_label_and_words(self) -> None:
        assert compare_answers("Ответ: 300 человек отдыхали\nв августе", "300") is True

    def test_units_in_parentheses(self) -> None:
        assert compare_answers("300 (чел.)", "300") is True

    def test_wrong_answer_in_sentence_is_wrong_not_uncertain(self) -> None:
        assert compare_answers("Ответ: 200 человек", "300") is False

    def test_compound_units_stay_uncertain(self) -> None:
        assert compare_answers("2 км 300 м", "2300") is None

    def test_label_without_number(self) -> None:
        assert compare_answers("Ответ: не знаю", "90") is None

    def test_dash_label_does_not_eat_negative_sign(self) -> None:
        assert compare_answers("Ответ -5 градусов", "-5") is True
        assert compare_answers("Ответ-5", "-5") is True
        assert compare_answers("Ответ - 5", "5") is True

    def test_old_forms_still_work(self) -> None:
        assert compare_answers("90 км/ч", "90") is True
        assert compare_answers("3 6/7", "27/7") is True
        assert compare_answers("4,5", "4.5") is True


class TestStepResultAnnotation:
    def test_result_with_units_and_hyphenated_tail_is_checked(self) -> None:
        checks = check_steps(["700 - (220 + 180) = 300 (чел.) - отдохну-"])
        assert checks[0].status == "ok"

    def test_unicode_minus_with_units(self) -> None:
        assert check_steps(["700 − (220 + 180) = 300 (чел.)"])[0].status == "ok"

    def test_wrong_result_with_units_is_mismatch(self) -> None:
        assert check_steps(["700 - (220 + 180) = 200 (чел.)"])[0].status == "mismatch"

    def test_words_inside_expression_are_not_cut(self) -> None:
        # «3 яблока + 2 = 5»: резать по первой букве нельзя — получилась бы ложная ошибка
        assert check_steps(["3 яблока + 2 = 5"])[0].status == "skipped"

    def test_tail_with_digits_is_not_cut(self) -> None:
        assert check_steps(["10 = 2 км 300 м"])[0].status == "skipped"
