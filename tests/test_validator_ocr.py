"""OCR читает «·» как «:» (живые логи 06.09, 08.09: в учебнике «·», в тетради «:»).

Печатное условие читается почти идеально — оно подсказывает, где в строке ученика
на самом деле умножение. Позиции сверяются по последовательности операторов.
"""

from hwcheck.bot.handlers import _validator_only_grade
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import check_steps

CONDITION = "15 · 10 + (30 − 20) · 5"


def status(line: str, condition: str | None) -> str:
    return check_steps([line], condition=condition)[0].status


def test_colon_under_printed_multiplication_is_reread() -> None:
    assert status("15 * 10 + (30 - 20) : 5 = 200", CONDITION) == "ok"
    assert status("15 * 10 + (30 - 20) : 5 = 200", None) == "mismatch"


def test_only_positions_with_printed_multiplication_are_reread() -> None:
    condition = "(120 + 320) : 4 · 2"
    assert status("(120 + 320) : 4 : 2 = 220", condition) == "ok"
    assert status("(120 + 320) * 4 * 2 = 55", condition) == "mismatch"


def test_real_error_stays_an_error() -> None:
    assert status("15 * 10 + (30 - 20) : 5 = 175", CONDITION) == "mismatch"


def test_printed_division_is_not_reread() -> None:
    assert status("120 : 4 = 480", "Вычисли: 120 : 4") == "mismatch"


def test_intermediate_line_with_other_operators_is_not_reread() -> None:
    assert status("150 + 10 : 5 = 200", CONDITION) == "mismatch"


def test_grade_and_validator_only_use_condition() -> None:
    steps = ["15 * 10 + (30 - 20) : 5 = 200"]
    ref = RefSolution(steps=["15 * 10 + (30 - 20) * 5 = 200"], answer="200")
    assert grade(steps, "200", ref, condition=CONDITION).slip_lines == []
    assert grade(steps, "200", ref).slip_lines == [1]  # без условия — «описка»
    assert _validator_only_grade(steps, condition=CONDITION).verdict == "correct"
    assert _validator_only_grade(steps).verdict == "wrong"
