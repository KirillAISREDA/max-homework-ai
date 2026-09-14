"""Уточняющие вопросы ученику, шаг 1: «не уверен» с понятной причиной → вопрос → пересчёт.

Вопросы не раскрывают эталон; ответ проверяется детерминированно, как исходная запись.
"""

import pytest

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.clarify import (
    MAX_QUESTIONS,
    apply_sign,
    apply_text,
    plan_clarifications,
    question,
)
from hwcheck.bot.fsm import CheckedTask, Clarification
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import RefSolution

REF = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")
CONDITION = "В лагере 700 ребят. В июне — 220, в июле — 180. Сколько в августе?"


def with_ref(steps: list[str], answer: str | None = None, number: int = 19) -> CheckedTask:
    task = VisionTask(
        number=number,
        task_text=CONDITION,
        student_solution_steps=steps,
        student_answer=answer,
        confidence=1,
    )
    return CheckedTask(task=task, ref=REF, grade=grade(steps, answer, REF, condition=CONDITION))


def without_ref(steps: list[str], number: int = 5) -> CheckedTask:
    task = VisionTask(number=number, task_text="", student_solution_steps=steps, confidence=1)
    return CheckedTask(task=task, ref=None, grade=validator_only_grade(steps))


BLURRED_SIGN = "15 * 10 + (30 - 20) <неразборчиво> 5 = 200"


# --- план вопросов ---


def test_plan_asks_answer_when_answer_missing_and_reference_known() -> None:
    item = with_ref(["220 + 180 = 400"])
    assert item.grade.uncertain_reason == "no_answer"
    assert plan_clarifications([item]) == [Clarification(task_index=0, kind="answer")]


def test_plan_asks_sign_or_line_for_unreadable_marks() -> None:
    sign = without_ref([BLURRED_SIGN])
    line = without_ref(["15 * 1<неразборчиво> = 150"], number=6)
    assert plan_clarifications([sign, line]) == [
        Clarification(task_index=0, kind="sign", line_index=0),
        Clarification(task_index=1, kind="line", line_index=0),
    ]


def test_plan_skips_what_a_question_cannot_fix() -> None:
    unparseable = without_ref(["Решение: смотри рисунок"])  # пробел проверки, не вопрос
    no_reference = without_ref(["220 + 180 = 400"])  # верно без эталона — вопрос не нужен
    wrong = without_ref(["2 + 2 = 5"])
    assert plan_clarifications([unparseable, no_reference, wrong]) == []


def test_plan_is_limited_per_homework() -> None:
    items = [without_ref([BLURRED_SIGN], number=n) for n in range(1, 5)]
    assert len(plan_clarifications(items)) == MAX_QUESTIONS == 2


# --- тексты вопросов ---


def test_answer_question_never_reveals_reference() -> None:
    item = with_ref(["220 + 180 = 400"])
    text, buttons = question(item, Clarification(task_index=0, kind="answer"))
    assert "№19" in text and "ответ" in text
    assert "300" not in text and buttons is None


def test_sign_question_shows_childs_line_and_sign_buttons() -> None:
    item = without_ref([BLURRED_SIGN])
    text, buttons = question(item, Clarification(task_index=0, kind="sign", line_index=0))
    assert "15 * 10 + (30 - 20) ? 5 = 200" in text
    assert buttons is not None
    assert [b["payload"] for row in buttons for b in row] == [
        "clarify:plus",
        "clarify:minus",
        "clarify:mul",
        "clarify:div",
    ]


# --- ответы ---


def test_answer_text_regrades_task() -> None:
    item = with_ref(["220 + 180 = 400"])
    clarification = Clarification(task_index=0, kind="answer")
    correct = apply_text(item, clarification, "Ответ: 300 человек")
    wrong = apply_text(item, clarification, "310")
    assert correct is not None and correct.grade.verdict == "correct"
    assert wrong is not None and wrong.grade.verdict == "wrong"
    assert apply_text(item, clarification, "не знаю") is None


def test_sign_button_regrades_line() -> None:
    item = without_ref([BLURRED_SIGN])
    clarification = Clarification(task_index=0, kind="sign", line_index=0)
    multiplied = apply_sign(item, clarification, "mul")
    added = apply_sign(item, clarification, "plus")
    assert multiplied is not None and multiplied.grade.verdict == "correct"
    assert added is not None and added.grade.verdict == "wrong"
    assert apply_sign(item, clarification, "power") is None  # payload недоверенный


@pytest.mark.parametrize(("typed", "verdict"), [("*", "correct"), ("·", "correct"), ("+", "wrong")])
def test_sign_can_be_typed(typed: str, verdict: str) -> None:
    item = without_ref([BLURRED_SIGN])
    result = apply_text(item, Clarification(task_index=0, kind="sign", line_index=0), typed)
    assert result is not None and result.grade.verdict == verdict


def test_retyped_line_replaces_unreadable_one() -> None:
    item = without_ref(["15 * 1<неразборчиво> = 150"])
    clarification = Clarification(task_index=0, kind="line", line_index=0)
    result = apply_text(item, clarification, "15 * 10 = 150")
    assert result is not None and result.grade.verdict == "correct"
    assert result.task.student_solution_steps == ["15 * 10 = 150"]
    assert apply_text(item, clarification, "там было десять") is None
