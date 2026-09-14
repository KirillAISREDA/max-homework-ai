"""Причина каждого «не уверен» — в журнале (шаг 0 перед уточняющими вопросами ученику).

64 % вердиктов 04–13.09 были «не уверен», но почему — не записывалось: нельзя понять,
какие уточняющие вопросы окупятся (почерк, знак, нет ответа) и что лечится кодом.
"""

from typing import Any

import pytest

from hwcheck.bot.handlers import _validator_only_grade
from hwcheck.events import summarize_events
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.solver import RefSolution

REF = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")


@pytest.mark.parametrize(
    ("steps", "answer", "reason"),
    [
        # на странице что-то нечитаемое — вопрос «какой здесь знак/цифра» мог бы помочь
        (["700 - (220 + <неразборчиво>) = 300"], None, "unreadable"),
        # (последняя строка — промежуточная: совпади она с эталоном, вердикт был бы «верно»)
        (["220 + 180 = 400"], "<неразборчиво>", "unreadable"),
        # ответ записан, но не парсится
        (["220 + 180 = 400"], "примерно триста человек и ещё двое", "answer_unparseable"),
        # ответа нет, арифметика верна, но последняя строка не эталон (могла быть промежуточной)
        (["220 + 180 = 400"], None, "no_answer"),
        # ни одна строка не разобрана и ответа нет
        (["Июнь — 220 чел., июль — 180 чел."], None, "steps_unparseable"),
    ],
)
def test_uncertain_with_reference_has_reason(
    steps: list[str], answer: str | None, reason: str
) -> None:
    result = grade(steps, answer, REF)
    assert (result.verdict, result.uncertain_reason) == ("uncertain", reason)


def test_certain_verdicts_have_no_reason() -> None:
    assert grade(["700 - 400 = 300"], "300", REF).uncertain_reason is None
    assert grade(["700 - 400 = 200"], "200", REF).uncertain_reason is None


@pytest.mark.parametrize(
    ("steps", "reason"),
    [
        (["2x + 4 = 18", "x - 3 = 5"], "ambiguous_equation"),
        (["Решение: смотри рисунок"], "steps_unparseable"),
        (["<неразборчиво>"], "unreadable"),
    ],
)
def test_uncertain_without_reference_has_reason(steps: list[str], reason: str) -> None:
    result = _validator_only_grade(steps)
    assert (result.verdict, result.uncertain_reason) == ("uncertain", reason)


def test_summary_counts_verdicts_and_reasons_by_environment() -> None:
    rows: list[dict[str, Any]] = [
        {"type": "task_checked", "env": "prod", "verdict": "correct"},
        {"type": "task_checked", "env": "prod", "verdict": "uncertain", "reason": "no_answer"},
        {"type": "task_checked", "env": "prod", "verdict": "uncertain", "reason": "no_answer"},
        {"type": "task_checked", "env": "prod", "verdict": "uncertain", "reason": "unreadable"},
        {"type": "task_checked", "env": "test", "verdict": "wrong"},
        {"type": "task_checked", "env": "dev", "verdict": "uncertain"},  # до шага 0 — без причины
        {"type": "vision_recognized", "env": "prod"},
    ]
    summary = summarize_events(rows)
    assert summary["prod"]["verdicts"] == {"correct": 1, "uncertain": 3}
    assert summary["prod"]["uncertain_reasons"] == {"no_answer": 2, "unreadable": 1}
    assert summary["test"]["verdicts"] == {"wrong": 1}
    assert summary["dev"]["uncertain_reasons"] == {"unknown": 1}
