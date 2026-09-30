"""Сводка ученику из находок: тексты те же, что были у математики (test_bot.py), плюс новые силы."""

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask, Clarification
from hwcheck.bot.summary import (
    MAX_MESSAGE_CHARS,
    NEXT_PHOTO,
    message_length,
    remaining_buttons,
    review_header,
    review_message,
    task_findings,
    task_line,
)
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
    assert text == "№7 — есть ошибка в строке «2 + 2 = 5» ❌"
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


# --- объяснение к верному заданию ---

PRAISE = "Ты правильно сложил единицы и не забыл перенести десяток: 803 + 169 = 972."


def praised(number: int, steps: list[str], praise: str = PRAISE) -> CheckedTask:
    return checked(number, steps).model_copy(update={"praise": praise})


def test_correct_line_carries_the_explanation() -> None:
    line, button = task_line(0, praised(17, ["803 + 169 = 972"]))
    assert line == f"№17 — верно ✅ {PRAISE}" and button is None


def test_explanation_is_shown_only_for_correct_task() -> None:
    """Текст остался от прошлого вердикта, а задание уже с ошибкой: хвалить нельзя."""
    line, _button = task_line(0, praised(7, ["2 + 2 = 5"]))
    assert line == "№7 — есть ошибка в строке «2 + 2 = 5» ❌"
    line, _button = task_line(0, praised(9, ["<неразборчиво>"]))
    assert line == "№9 — часть записи неразборчива 🤔"


def test_review_message_joins_lines_and_buttons() -> None:
    state = ChatState(
        phase="clarifying",
        tasks=[
            praised(17, ["803 + 169 = 972"]),
            checked(18, ["2 + 2 = 5"]),
            checked(19, ["15 <неразборчиво> 10 = 150"]),
        ],
        clarifications=[Clarification(task_index=2, kind="sign", line_index=0)],
    )
    text, buttons = review_message(state)
    assert text == (
        "Проверил! 1 из 2 верно.\n"  # задание с вопросом ещё без вердикта
        f"№17 — верно ✅ {PRAISE}\n"
        "№18 — есть ошибка в строке «2 + 2 = 5» ❌\n"
        "№19 — уточню у тебя одну деталь ✍️"
    )
    assert [row[0]["payload"] for row in buttons] == ["tutor:1"]


def test_long_review_drops_explanations_from_the_end_but_keeps_verdicts() -> None:
    """Лимит сообщения MAX: без объяснения ребёнок проживёт, без вердикта — нет."""
    praise = "Ты правильно сложил единицы и десятки. " * 5
    tasks = [praised(n, ["2 + 2 = 4"], praise.strip()) for n in range(1, 31)]
    text, _buttons = review_message(ChatState(phase="review", tasks=tasks))

    lines = text.splitlines()
    assert message_length(text) <= MAX_MESSAGE_CHARS
    assert lines[0] == "Проверил! 30 из 30 верно."
    assert lines[-1] == NEXT_PHOTO  # ошибок нет — сводка говорит, что дальше
    tasks_lines = lines[1:-1]
    assert [line.split(" — ")[0] for line in tasks_lines] == [f"№{n}" for n in range(1, 31)]
    assert all(line.startswith(f"№{n} — верно ✅") for n, line in enumerate(tasks_lines, 1))
    explained = [praise.strip() in line for line in tasks_lines]
    kept = sum(explained)
    assert 0 < kept < 30
    assert explained == [True] * kept + [False] * (30 - kept)  # опущены с конца
    assert tasks_lines[-1] == "№30 — верно ✅"


def test_message_length_counts_utf16_units() -> None:
    # 🤔 — два элемента UTF-16: считаем с запасом, чтобы сообщение точно прошло лимит
    assert message_length("№9 🤔") == 5
