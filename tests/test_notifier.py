"""Итог проверки родителю (спецификация онбординга §9.1, §9.4): тексты, счётчики, подпись ребёнка.

В уведомлении — только класс, предмет, счётчики и номера заданий с ошибкой: ни текста заданий,
ни ответов, ни фото, ни имени.
"""

import pytest

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.notifier import (
    SUBJECT_DATIVE,
    HomeworkSummary,
    checked_text,
    child_label,
    plural,
    resolved_text,
    summarize,
    switch_keyboard,
)
from hwcheck.bot.subjects import SUBJECTS
from hwcheck.db.repo import HomeworkCounts, StudentProfile
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Finding

CHILD = "Ребёнок (7 класс)"


def checked(
    number: int,
    steps: list[str],
    findings: list[Finding] | None = None,
    *,
    on_page: bool = True,
) -> CheckedTask:
    task = VisionTask(
        number=number,
        task_text="Сколько яблок у Пети?",
        student_solution_steps=steps,
        student_answer="семь яблок",
        confidence=1,
        number_on_page=on_page,
    )
    return CheckedTask(
        task=task, ref=None, grade=validator_only_grade(steps), findings=findings or []
    )


def profile(profile_id: int, grade: int, *, own: bool = True) -> StudentProfile:
    return StudentProfile(
        id=profile_id,
        user_id=100 + profile_id if own else None,
        parent_user_id=9,
        grade=grade,
        grade_year=2026,
        subject="math",
        has_consent=True,
    )


def summary(total: int, correct: int, wrong: int, uncertain: int, *labels: str) -> HomeworkSummary:
    return HomeworkSummary(HomeworkCounts(total, correct, wrong, uncertain), list(labels))


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 заданий"),
        (1, "1 задание"),
        (2, "2 задания"),
        (4, "4 задания"),
        (5, "5 заданий"),
        (10, "10 заданий"),
        (11, "11 заданий"),
        (12, "12 заданий"),
        (14, "14 заданий"),
        (21, "21 задание"),
        (22, "22 задания"),
        (25, "25 заданий"),
        (101, "101 задание"),
        (111, "111 заданий"),
        (112, "112 заданий"),
        (122, "122 задания"),
    ],
)
def test_plural_follows_russian_numerals(n: int, expected: str) -> None:
    assert plural(n, "задание", "задания", "заданий") == expected


def test_summary_counts_tasks_by_worst_finding() -> None:
    rejected = Finding(task_index=4, kind="spelling", strength="candidate", confirmed=False)
    essay = Finding(task_index=5, kind="essay", strength="feedback")
    state = ChatState(
        phase="review",
        tasks=[
            checked(16, ["2 + 2 = 4"]),
            checked(19, ["2 + 2 = 5"]),
            checked(2, ["3 + 3 = 7"], on_page=False),
            checked(21, ["<неразборчиво>"]),
            checked(22, [], [rejected]),  # ребёнок ответил «нет» — находка снята
            checked(23, [], [essay]),  # оценки нет: ни «верно», ни «перепроверить»
        ],
    )
    assert summarize(state) == summary(6, 2, 2, 1, "№19", "задание 2")


def test_checked_text_matches_specification_example() -> None:
    text = checked_text(CHILD, "math", summary(4, 3, 1, 0, "№19"))
    assert text == (
        "Ребёнок (7 класс) проверил домашку по математике: 4 задания — 3 верно, "
        "1 с ошибкой (№19), разбирает с подсказками."
    )


@pytest.mark.parametrize(
    ("counts", "tail"),
    [
        (summary(4, 4, 0, 0), "4 задания — все верно ✅"),
        (summary(1, 1, 0, 0), "1 задание — верно ✅"),
        (summary(1, 0, 1, 0, "№19"), "1 задание — с ошибкой (№19), разбирает с подсказками."),
        (summary(1, 0, 0, 1), "1 задание — стоит перепроверить."),
        (summary(1, 0, 0, 0), "1 задание — без оценки."),
        (summary(5, 4, 0, 1), "5 заданий — 4 верно, 1 стоит перепроверить."),
        (
            summary(2, 0, 2, 0, "№1", "задание 2"),
            "2 задания — 2 с ошибкой (№1, задание 2), разбирает с подсказками.",
        ),
        (
            summary(21, 17, 2, 1, "№5", "№7"),
            "21 задание — 17 верно, 1 стоит перепроверить, 1 без оценки, "
            "2 с ошибкой (№5, №7), разбирает с подсказками.",
        ),
    ],
)
def test_checked_text_variants(counts: HomeworkSummary, tail: str) -> None:
    assert checked_text(CHILD, "math", counts) == (
        f"Ребёнок (7 класс) проверил домашку по математике: {tail}"
    )


def test_doubt_is_never_called_an_error() -> None:
    """Инвариант «при сомнении — не уверен, а не ошибка» действует и для родителя."""
    text = checked_text(CHILD, "math", summary(3, 2, 0, 1))
    assert "стоит перепроверить" in text
    assert "ошиб" not in text and "разбирает" not in text


def test_notification_carries_no_homework_content() -> None:
    state = ChatState(phase="review", tasks=[checked(19, ["2 + 2 = 5"]), checked(20, ["1+1=2"])])
    text = checked_text(CHILD, "math", summarize(state))
    for leaked in ("яблок", "Пети", "семь", "2 + 2", "= 5", "1+1"):
        assert leaked not in text


def test_resolved_text() -> None:
    assert resolved_text(CHILD, "math") == (
        "Ребёнок (7 класс) разобрал все ошибки в домашке по математике ✅"
    )
    assert resolved_text("Ребёнок 2 (5 класс)", "russian") == (
        "Ребёнок 2 (5 класс) разобрал все ошибки в домашке по русскому языку ✅"
    )


def test_unknown_subject_is_left_out_of_the_text() -> None:
    assert checked_text(CHILD, "astronomy", summary(1, 1, 0, 0)) == (
        "Ребёнок (7 класс) проверил домашку: 1 задание — верно ✅"
    )
    assert resolved_text(CHILD, "astronomy") == "Ребёнок (7 класс) разобрал все ошибки в домашке ✅"


def test_every_catalog_subject_has_dative_form() -> None:
    assert set(SUBJECT_DATIVE) == {subject.code for subject in SUBJECTS}
    assert all(form == form.lower() and form for form in SUBJECT_DATIVE.values())


def test_child_label_numbers_children_of_same_grade() -> None:
    first, other, second = profile(1, 7), profile(2, 5), profile(3, 7)
    family = [first, other, second]
    assert child_label(first, family) == "Ребёнок 1 (7 класс)"
    assert child_label(second, family) == "Ребёнок 2 (7 класс)"
    assert child_label(other, family) == "Ребёнок (5 класс)"
    assert child_label(first, [first]) == "Ребёнок (7 класс)"
    assert child_label(first, []) == "Ребёнок (7 класс)"  # список детей не прочитался


def test_switch_keyboard_offers_the_opposite() -> None:
    assert switch_keyboard(enabled=True) == [
        [{"type": "callback", "text": "Не присылать итоги", "payload": "ob:notify:off"}]
    ]
    assert switch_keyboard(enabled=False) == [
        [{"type": "callback", "text": "Присылать итоги", "payload": "ob:notify:on"}]
    ]


def test_report_button_is_second_row_under_instant_notification() -> None:
    assert switch_keyboard(enabled=True, report=True) == [
        [{"type": "callback", "text": "Не присылать итоги", "payload": "ob:notify:off"}],
        [{"type": "callback", "text": "📈 Отчёт о прогрессе", "payload": "ob:report"}],
    ]
    # под подтверждением отключения — только кнопка «обратно»
    assert switch_keyboard(enabled=False, report=True) == switch_keyboard(enabled=False)
