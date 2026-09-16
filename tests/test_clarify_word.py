"""Уточняющий вопрос «здесь написано …?» с кропом слова (спецификация каркаса §8)."""

import io
from pathlib import Path

from PIL import Image

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.clarify import apply_word, plan_clarifications, question
from hwcheck.bot.crops import crop_word
from hwcheck.bot.fsm import CheckedTask
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Box, Finding, Word


def test_crop_word_adds_margin_and_clamps() -> None:
    image = Image.new("RGB", (100, 50), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    crop = Image.open(io.BytesIO(crop_word(buffer.getvalue(), Box(x0=5, y0=5, x1=40, y1=20))))
    assert crop.size == (52, 32)  # поля 12 px, обрезано по краю изображения слева/сверху


def item_with_word() -> CheckedTask:
    task = VisionTask(number=3, task_text="", student_solution_steps=[], confidence=1)
    word = Word(text="машына", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.4)
    finding = Finding(
        task_index=0, kind="spelling", strength="candidate", actual="машына", word=word
    )
    return CheckedTask(task=task, ref=None, grade=validator_only_grade([]), findings=[finding])


def test_word_question_and_answers() -> None:
    item = item_with_word()
    [clarification] = plan_clarifications([item])
    assert (clarification.kind, clarification.finding_index) == ("word", 0)
    text, buttons = question(item, clarification)
    assert text == "№3: здесь написано «машына»?"
    assert buttons is not None and [b["payload"] for b in buttons[0]] == [
        f"clarify:{clarification.token}:yes",
        f"clarify:{clarification.token}:no",
    ]
    confirmed = apply_word(item, clarification, "yes")
    assert confirmed is not None and confirmed.findings[0].confirmed is True
    denied = apply_word(item, clarification, "no")
    assert denied is not None and denied.findings[0].confirmed is False
    assert apply_word(item, clarification, "maybe") is None


def test_word_questions_share_limit_with_math(tmp_path: Path) -> None:
    unsure = VisionTask(
        number=1, task_text="", student_solution_steps=["<неразборчиво>"], confidence=1
    )
    math_item = CheckedTask(task=unsure, ref=None, grade=validator_only_grade(["<неразборчиво>"]))
    plan = plan_clarifications([math_item, item_with_word(), item_with_word()])
    assert [c.kind for c in plan] == ["line", "word"]  # MAX_QUESTIONS = 2
