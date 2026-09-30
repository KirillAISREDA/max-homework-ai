"""Вопрос «какой ответ получился в пункте а)?» вместо «не уверен» (спецификация
2026-09-30-math-misread-guards-design.md §4).

Строка, прочитанная не так, как напечатан её пункт, — не ошибка ребёнка. Спрашиваем результат
и пересчитываем его против печатного выражения: эталон вопрос не раскрывает.
"""

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.clarify import (
    MAX_QUESTIONS,
    answer_reply,
    apply_text,
    plan_clarifications,
    question,
    retry_prompt,
)
from hwcheck.bot.fsm import CheckedTask, Clarification
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.math.module import findings_from_grade
from test_reading import CONDITION_2181, STEPS_2181


def live_task(steps: list[str] | None = None) -> CheckedTask:
    lines = STEPS_2181 if steps is None else steps
    task = VisionTask(
        number=2181,
        task_text=CONDITION_2181,
        student_solution_steps=lines,
        confidence=1,
        number_on_page=False,
    )
    return CheckedTask(
        task=task, ref=None, grade=validator_only_grade(lines, condition=CONDITION_2181)
    )


def result_question(line_index: int) -> Clarification:
    return Clarification(task_index=0, kind="result", line_index=line_index)


def test_misread_lines_get_printed_expression() -> None:
    checks = live_task().grade.line_checks
    assert checks[0].printed == "39452 - 16452 : (300 - 264)"
    assert checks[1].printed == "2558304 : 63 + 1662372 : 61"
    assert checks[2].printed is None  # верная строка — спрашивать нечего


def test_plan_asks_result_of_each_misread_item_within_limit() -> None:
    planned = plan_clarifications([live_task()])
    assert [(c.task_index, c.kind, c.line_index) for c in planned] == [
        (0, "result", 0),
        (0, "result", 1),
    ]
    assert len(planned) == MAX_QUESTIONS


def test_misread_line_without_printed_item_is_asked_to_retype() -> None:
    task = VisionTask(
        number=5,
        task_text="",
        student_solution_steps=["93601 - 667 * (231 - 88) = 7658"],
        confidence=1,
    )
    item = CheckedTask(task=task, ref=None, grade=validator_only_grade(task.student_solution_steps))
    [planned] = plan_clarifications([item])
    assert (planned.kind, planned.line_index) == ("line", 0)


def test_result_question_shows_printed_example_but_not_its_value() -> None:
    text, buttons = question(live_task(), result_question(0))
    assert "пункт а)" in text
    assert "39452 − 16452 : (300 − 264)" in text
    assert "числом" in text
    assert "38995" not in text and buttons is None


def test_wrong_typed_result_becomes_verified_error_with_printed_target() -> None:
    updated = apply_text(live_task(), result_question(0), "38997")
    assert updated is not None
    assert updated.task.student_solution_steps[0] == "а) 39452 - 16452 : (300 - 264) = 38997"
    assert (updated.grade.verdict, updated.grade.first_error_line) == ("wrong", 1)
    [finding] = findings_from_grade(0, updated.grade)
    assert (finding.strength, finding.expected) == ("verified", "38995")


def test_right_typed_result_makes_the_item_correct() -> None:
    updated = apply_text(live_task(), result_question(0), "Ответ: 38995")
    assert updated is not None
    assert updated.grade.line_checks[0].status == "ok"
    assert updated.grade.verdict == "uncertain"  # пункт б) всё ещё под вопросом
    both = apply_text(updated, result_question(1), "67860")
    assert both is not None and both.grade.verdict == "correct"


def test_result_matching_childs_own_record_is_not_called_an_error() -> None:
    # ребёнок переписал 39454 вместо 39452 и свой пример посчитал верно — или условие прочиталось
    # неверно: ошибку не утверждаем, просим сверить запись
    item = live_task(["а) 39454 - 16452 : (300 - 264) = 38997"])
    updated = apply_text(item, result_question(0), "38997")
    assert updated is not None and updated.grade is not None
    assert updated.grade.verdict == "uncertain"
    check = updated.grade.line_checks[0]
    assert (check.misread, check.printed) == (True, "39452 - 16452 : (300 - 264)")
    text, button = answer_reply(result_question(0), updated, more_for_task=False)
    assert "сходится с твоей записью" in text and "39452 − 16452 : (300 − 264)" in text
    assert button is None


def test_unclear_reply_is_not_understood() -> None:
    assert apply_text(live_task(), result_question(0), "не знаю") is None
    assert "только число" in retry_prompt(result_question(0))[0]


def test_reply_names_the_item_and_waits_for_the_rest_of_the_task() -> None:
    updated = apply_text(live_task(), result_question(0), "38997")
    assert updated is not None
    text, button = answer_reply(result_question(0), updated, more_for_task=True)
    assert (text, button) == ("Пункт а) — есть ошибка ❌", None)
    final = apply_text(updated, result_question(1), "67860")
    assert final is not None
    text, button = answer_reply(result_question(1), final, more_for_task=False)
    assert text.startswith("Пункт б) — верно ✅\n")
    assert "есть ошибка" in text
    assert button is not None and button[0]["payload"] == "tutor:0"
