"""Сводка ученику из находок: тексты те же, что были у математики (test_bot.py), плюс новые силы."""

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.summary import remaining_buttons, review_header, task_findings, task_line
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Box, Finding, SubjectTask, Word
from hwcheck.subjects.math.module import to_vision_task


def checked(number: int, steps: list[str], findings: list[Finding] | None = None) -> CheckedTask:
    task = VisionTask(number=number, task_text="", student_solution_steps=steps, confidence=1)
    return CheckedTask(
        task=task, ref=None, grade=validator_only_grade(steps), findings=findings or []
    )


def test_math_lines_unchanged() -> None:
    assert task_line(0, checked(4, ["2 + 2 = 4"])) == ("№4 — верно ✅", None)
    text, button = task_line(1, checked(7, ["2 + 2 = 5"]))
    assert text == "№7 — есть ошибка (строка 1) ❌"
    assert button == [{"type": "callback", "text": "Разобрать №7", "payload": "tutor:1"}]
    assert task_line(2, checked(9, ["<неразборчиво>"])) == (
        "№9 — часть записи неразборчива 🤔",
        None,
    )


def test_explicit_findings_override_grade() -> None:
    word = Finding(task_index=0, kind="spelling", strength="candidate", actual="машына")
    item = checked(3, ["2 + 2 = 5"], findings=[word])
    assert task_findings(0, item) == [word]
    assert task_line(0, item) == ("№3 — стоит перепроверить 🤔 (1 место)", None)
    essay = Finding(task_index=0, kind="essay", strength="feedback")
    assert task_line(0, checked(3, [], findings=[essay])) == ("№3 — разобрал, оценки нет 📝", None)
    confirmed = word.model_copy(update={"confirmed": True})
    text, button = task_line(0, checked(3, [], findings=[confirmed]))
    assert text == "№3 — есть ошибка (слово «машына») ❌" and button is not None


def test_task_line_for_language_task_without_grade() -> None:
    word = Word(text="позняя", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.8, line=1)
    finding = Finding(task_index=0, kind="spelling", strength="candidate", actual="позняя",
                      expected="поздняя", word=word, detail="проверь слово «позняя»")  # fmt: skip
    task = SubjectTask(number="245", words=[word])
    item = CheckedTask(task=to_vision_task(task), ref=None, subject_task=task, findings=[finding])
    line, button = task_line(0, item)
    assert line == "№245 — проверь слово «позняя» 🤔" and button is None
    confirmed = item.model_copy(
        update={"findings": [finding.model_copy(update={"confirmed": True})]}
    )
    line, button = task_line(0, confirmed)
    assert line == "№245 — есть ошибка (слово «позняя») ❌" and button is not None


def test_review_header_counts_language_tasks() -> None:
    task = SubjectTask(number="1", words=[])
    state = ChatState(tasks=[CheckedTask(task=to_vision_task(task), ref=None, subject_task=task)])
    assert review_header(state) == "Проверил! 1 из 1 верно.\n"


def test_header_and_remaining_buttons() -> None:
    state = ChatState(
        phase="review",
        tasks=[checked(4, ["2 + 2 = 5"]), checked(5, ["1 + 1 = 2"]), checked(6, ["3 + 3 = 7"])],
        resolved_indices=[0],
    )
    assert review_header(state) == "Проверил! 1 из 3 верно.\n"
    assert [b[0]["payload"] for b in remaining_buttons(state)] == ["tutor:2"]
