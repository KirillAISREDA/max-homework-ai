"""Уравнения с переменной (живые логи 07.09, №462 — всегда «не уверен»).

Преобразование уравнения сохраняет корень: строка, после которой корень
изменился, — ошибочная. Цепочка заканчивается строкой «x = число».
"""

import pytest

from hwcheck.bot.handlers import _pseudo_ref, _validator_only_grade
from hwcheck.bot.pages import page_role
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.mathparse import parse_value
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import check_steps, compare_answers


def statuses(steps: list[str]) -> list[str]:
    return [c.status for c in check_steps(steps)]


def test_correct_solution_chain_is_ok() -> None:
    checks = check_steps(["x + 5 = 12", "x = 12 - 5", "x = 7"])
    assert [c.status for c in checks] == ["ok", "ok", "ok"]
    assert checks[-1].values[-1] == "7"


def test_wrong_transformation_is_the_error_line() -> None:
    checks = check_steps(["x + 5 = 12", "x = 12 + 5", "x = 17"])
    assert [c.status for c in checks] == ["ok", "mismatch", "ok"]
    assert checks[1].values[0] == "7"  # верный корень — цель для тьютора


def test_arithmetic_error_in_final_line() -> None:
    assert statuses(["x + 5 = 12", "x = 12 - 5", "x = 8"]) == ["ok", "ok", "mismatch"]


def test_two_equations_in_one_task_are_separate_chains() -> None:
    assert statuses(["x + 2 = 7", "x = 5", "x - 1 = 2", "x = 3"]) == ["ok"] * 4


@pytest.mark.parametrize(
    "steps",
    [
        ["96 : x = 8", "x = 96 : 8", "x = 12"],
        ["3x + 4 = 19", "3x = 15", "x = 5"],
        ["х + 5 = 12", "х = 7"],  # кириллическая «х»
        ["(x - 3) * 2 = 10", "x - 3 = 5", "x = 8"],
        ["y : 4 = 2,5", "y = 2,5 * 4", "y = 10"],
    ],
)
def test_school_equation_notations(steps: list[str]) -> None:
    assert statuses(steps) == ["ok"] * len(steps)


def test_numeric_chain_inside_equation_line_is_checked() -> None:
    assert statuses(["x + 5 = 12", "x = 12 - 5 = 8"]) == ["ok", "mismatch"]


@pytest.mark.parametrize(
    "line",
    ["Дано: a=5, b=3", "sin α = y/r", "x = 54 − 2y", "x^2 = 9", "S = a * b", "x = x"],
)
def test_not_checkable_lines_stay_skipped(line: str) -> None:
    # несколько переменных, два корня, тождество — честнее не проверять, чем ошибиться
    assert statuses([line]) == ["skipped"]


def test_answer_with_variable_is_parsed() -> None:
    assert parse_value("x = 7") == 7
    assert parse_value("Ответ: х = 7") == 7
    assert compare_answers("x=7", "7") is True


def test_grade_equation_task() -> None:
    ref = RefSolution(steps=["x = 12 - 5", "x = 7"], answer="7")
    assert grade(["x + 5 = 12", "x = 12 - 5", "x = 7"], "x = 7", ref).verdict == "correct"
    wrong = grade(["x + 5 = 12", "x = 12 + 5", "x = 17"], "x = 17", ref)
    assert (wrong.verdict, wrong.first_error_line) == ("wrong", 2)


def test_validator_only_equation_gives_tutor_target() -> None:
    result = _validator_only_grade(["x + 5 = 12", "x = 12 + 5", "x = 17"])
    assert (result.verdict, result.first_error_line) == ("wrong", 2)
    assert _pseudo_ref(result).answer == "7"


def test_textbook_page_with_equations_stays_textbook() -> None:
    # «Реши уравнения» в учебнике: уравнения без решения не делают страницу тетрадью
    tasks = [
        VisionTask(number=n, task_text="Реши уравнение", student_solution_steps=[eq], confidence=1)
        for n, eq in ((1, "x + 5 = 12"), (2, "96 : x = 8"), (3, "3x + 4 = 19"))
    ]
    assert page_role(VisionPage(tasks=tasks, page_ok=True)) == "textbook"
