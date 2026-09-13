"""Вычисления столбиком (живые логи 07.09 №23, 08.09 — всегда «не уверен»).

Транскрипция пишет столбик построчно: операнд, знак с операндом (или знак
отдельной строкой), черта, результат. Проверяется строка результата.
"""

import pytest

from hwcheck.bot.handlers import _validator_only_grade
from hwcheck.pipeline.validator import check_steps


def statuses(steps: list[str]) -> list[str]:
    return [c.status for c in check_steps(steps)]


@pytest.mark.parametrize(
    "steps",
    [
        ["803", "+169", "972"],
        ["803", "+ 169", "-----", "972"],
        ["+803", "169", "____", "972"],
        ["1000", "-", "358", "642"],
        ["1000", "− 358", "642"],
        ["4,5", "+3,25", "7,75"],
        ["45", "× 3", "---", "135"],
    ],
)
def test_correct_column_result_is_ok(steps: list[str]) -> None:
    assert statuses(steps)[-1] == "ok"
    assert set(statuses(steps)[:-1]) == {"skipped"}


def test_wrong_column_result_is_mismatch_with_correct_value() -> None:
    checks = check_steps(["803", "+ 169", "-----", "753"])
    assert checks[-1].status == "mismatch"
    assert checks[-1].values == ["972", "753"]


def test_two_columns_in_a_row() -> None:
    steps = ["803", "+169", "972", "425", "+375", "800"]
    assert statuses(steps) == ["skipped", "skipped", "ok", "skipped", "skipped", "ok"]


@pytest.mark.parametrize(
    "steps",
    [
        ["803", "169", "972"],  # без знака — просто числа на странице
        ["№ 23", "972"],
        ["803 + 169 = 972", "972"],
        ["23", "* 45", "115", "92", "1035"],  # умножение с промежуточными произведениями
    ],
)
def test_not_a_column(steps: list[str]) -> None:
    checks = check_steps(steps)
    assert [c.status for c in checks if c.line in ("972", "1035")] in (["skipped"], [])


@pytest.mark.parametrize(
    "steps",
    [
        ["5", "-3", "3"],  # три отдельных ответа, не столбик (ревью)
        ["45", "× 3", "130"],  # однозначный операнд без черты — не узнаём
        ["12", "+7", "20"],
    ],
)
def test_short_operands_without_rule_are_not_a_column(steps: list[str]) -> None:
    assert set(statuses(steps)) == {"skipped"}


def test_column_error_makes_task_wrong() -> None:
    result = _validator_only_grade(["803", "+169", "753"])
    assert (result.verdict, result.first_error_line) == ("wrong", 3)
