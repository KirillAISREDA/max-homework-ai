"""Уточняющий вопрос «здесь написано …?» с кропом слова (спецификация каркаса §8)."""

import io
from pathlib import Path

from PIL import Image

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.clarify import (
    _finding,
    apply_text,
    apply_word,
    plan_clarifications,
    question,
    regrade,
)
from hwcheck.bot.crops import crop_word
from hwcheck.bot.fsm import CheckedTask, Clarification
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Box, Finding, SubjectTask, Word
from hwcheck.subjects.math.module import to_vision_task


def test_crop_word_adds_margin_and_clamps() -> None:
    image = Image.new("RGB", (100, 50), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    crop = Image.open(io.BytesIO(crop_word(buffer.getvalue(), Box(x0=5, y0=5, x1=40, y1=20))))
    assert crop.size == (52, 32)  # поля 12 px, обрезано по краю изображения слева/сверху


def test_crop_word_applies_exif_rotation_like_ocr() -> None:
    """OCR (`ocrsvc/engine.py`) отдаёт координаты уже развёрнутого по EXIF кадра — кроп обязан
    развернуть фото так же, иначе у снятого «лёжа» фото ребёнок видит не то слово (ревью, I3)."""
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # ориентация 6 = поворот на 90°: пиксели хранятся повернутыми
    Image.new("RGB", (100, 50), "white").save(buffer, format="JPEG", exif=exif.tobytes())
    # после разворота кадр вертикальный (50×100) — бокс у правого края развёрнутого кадра
    crop = Image.open(io.BytesIO(crop_word(buffer.getvalue(), Box(x0=20, y0=80, x1=45, y1=95))))
    assert crop.size == (42, 32)  # 20−12…45+12 по ширине 50, 80−12…95+12 по высоте 100


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
    assert (clarification.kind, clarification.finding_id) == ("word", item.findings[0].id)
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


def test_extra_and_missing_word_findings_are_not_asked_about() -> None:
    """«Здесь написано «17»?» про лишнее слово: честное «да» подтверждает не-ошибку, а кнопка
    «Разобрать» упирается в «нечего разбирать». Такие находки остаются в сводке как «стоит
    перепроверить», вопроса по ним нет (финальное ревью 17.09, I1)."""
    task = SubjectTask(number="1", words=[])
    word = Word(text="17", box=Box(x0=0, y0=0, x1=9, y1=9))
    extra = Finding(task_index=0, kind="extra_word", strength="candidate", actual="17", word=word)
    missing = Finding(
        task_index=0, kind="missing_word", strength="candidate", expected="нас", word=word
    )
    item = CheckedTask(task=to_vision_task(task), ref=None, subject_task=task,
                       findings=[extra, missing])  # fmt: skip
    assert plan_clarifications([item]) == []


def test_plan_clarifications_skips_grade_when_missing() -> None:
    """Языки идут без пересчёта: `grade` у задания нет, вопрос ставится по находке."""
    task = SubjectTask(number="1", words=[])
    word = Word(text="а", box=Box(x0=0, y0=0, x1=1, y1=1))
    item = CheckedTask(task=to_vision_task(task), ref=None, subject_task=task,
                       findings=[Finding(task_index=0, kind="spelling", strength="candidate",
                                         actual="а", word=word)])  # fmt: skip
    [clarification] = plan_clarifications([item])
    assert clarification.kind == "word"


def test_word_questions_share_limit_with_math(tmp_path: Path) -> None:
    unsure = VisionTask(
        number=1, task_text="", student_solution_steps=["<неразборчиво>"], confidence=1
    )
    math_item = CheckedTask(task=unsure, ref=None, grade=validator_only_grade(["<неразборчиво>"]))
    plan = plan_clarifications([math_item, item_with_word(), item_with_word()])
    assert [c.kind for c in plan] == ["line", "word"]  # MAX_QUESTIONS = 2


def test_word_apply_text_accepts_da_net() -> None:
    item = item_with_word()
    [clarification] = plan_clarifications([item])
    confirmed = apply_text(item, clarification, "Да")
    assert confirmed is not None and confirmed.findings[0].confirmed is True
    denied = apply_text(item, clarification, "нет")
    assert denied is not None and denied.findings[0].confirmed is False
    assert apply_text(item, clarification, "может быть") is None


def test_regrade_preserves_word_finding_and_replaces_math() -> None:
    """Пересчёт другого (математического) вопроса той же задачи не должен терять word-находку —
    иначе очередная word-clarification указывает на чужую находку (code review 17.09)."""
    task = VisionTask(
        number=1, task_text="", student_solution_steps=["<неразборчиво>"], confidence=1
    )
    math_finding = Finding(
        task_index=0, kind="uncertain", strength="candidate", detail="не уверен в проверке"
    )
    word = Word(text="машына", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.4)
    word_finding = Finding(
        task_index=0, kind="spelling", strength="candidate", actual="машына", word=word
    )
    item = CheckedTask(
        task=task,
        ref=None,
        grade=validator_only_grade(["<неразборчиво>"]),
        findings=[math_finding, word_finding],
    )
    fixed = task.model_copy(update={"student_solution_steps": ["2 + 2 = 5"]})

    result = regrade(item, fixed, 0)

    assert result.grade.verdict == "wrong"
    assert result.findings[1] == word_finding  # word-находка осталась на своём месте
    assert result.findings[0].kind == "arithmetic" and result.findings[0].strength == "verified"


def test_finding_returns_none_for_unknown_id() -> None:
    item = item_with_word()
    clarification = Clarification(task_index=0, kind="word", finding_id="deadbeef")
    assert _finding(item, clarification) is None


def test_word_finding_found_by_id_after_regrade_drops_math_finding() -> None:
    """Пересчёт убрал математическую находку — word-находка сдвинулась, но вопрос к ней
    привязан по `id`, а не по позиции в списке (финальное ревью 17.09, F9)."""
    task = VisionTask(
        number=1, task_text="", student_solution_steps=["<неразборчиво>"], confidence=1
    )
    math_finding = Finding(
        task_index=0, kind="uncertain", strength="candidate", detail="не уверен в проверке"
    )
    word = Word(text="машына", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.4)
    word_finding = Finding(
        task_index=0, kind="spelling", strength="candidate", actual="машына", word=word
    )
    item = CheckedTask(
        task=task,
        ref=None,
        grade=validator_only_grade(["<неразборчиво>"]),
        findings=[math_finding, word_finding],
    )
    clarification = Clarification(task_index=0, kind="word", finding_id=word_finding.id)

    result = regrade(item, task.model_copy(update={"student_solution_steps": ["2 + 2 = 4"]}), 0)

    assert [f.id for f in result.findings] == [word_finding.id]  # математической находки не стало
    assert _finding(result, clarification) == word_finding
    confirmed = apply_word(result, clarification, "yes")
    assert confirmed is not None and confirmed.findings[0].confirmed is True
