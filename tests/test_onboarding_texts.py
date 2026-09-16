"""Тексты и клавиатуры онбординга, политика сообщениями MAX (спецификация §4, §10.3)."""

import pytest

from hwcheck.bot.max_api import Buttons
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import (
    MAX_MESSAGE_LEN,
    POLICY_VERSION,
    policy_messages,
    split_message,
)
from hwcheck.db.repo import StudentProfile


def payloads(buttons: Buttons) -> list[str]:
    return [button["payload"] for row in buttons for button in row]


def profile(profile_id: int, grade: int, *, own: bool = False) -> StudentProfile:
    return StudentProfile(
        id=profile_id,
        user_id=100 + profile_id if own else None,
        parent_user_id=9,
        grade=grade,
        grade_year=2026,
        subject="math",
        has_consent=True,
    )


def test_split_message_by_paragraphs_and_limit() -> None:
    a, b = "а" * 10, "б" * 10
    assert split_message(f"{a}\n\n{b}", limit=15) == [a, b]
    assert split_message(f"{a}\n\n\n\n{b}", limit=25) == [f"{a}\n\n{b}"]
    assert split_message("в" * 35, limit=15) == ["в" * 15, "в" * 15, "в" * 5]


def test_policy_fits_max_messages() -> None:
    messages = policy_messages()
    assert messages and all(0 < len(m) <= MAX_MESSAGE_LEN for m in messages)
    assert POLICY_VERSION in messages[0]
    assert "GigaChat" in "\n".join(messages)


def test_consent_summary_names_transfer_and_version() -> None:
    summary = texts.CONSENT_SUMMARY
    assert "GigaChat API (ПАО Сбербанк)" in summary and POLICY_VERSION in summary
    text = texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    assert text.startswith("Ваш ребёнок (7 класс)") and len(text) <= MAX_MESSAGE_LEN


def test_grade_and_role_keyboards() -> None:
    assert payloads(texts.role_keyboard()) == ["ob:role:student", "ob:role:parent"]
    grades = texts.grade_keyboard("pgrade")
    assert [len(row) for row in grades] == [3, 3, 3]
    assert payloads(grades) == [f"ob:pgrade:{g}" for g in range(1, 10)]


def test_subject_keyboard_marks_unavailable_and_fits_grade() -> None:
    buttons = texts.subject_keyboard(3)
    titles = {b["payload"]: b["text"] for row in buttons for b in row}
    assert titles["ob:subject:math"] == "Математика"
    assert titles["ob:subject:world_around"] == "🔜 Окружающий мир"
    assert "ob:subject:history" not in titles
    assert all(len(row) <= 2 for row in buttons)


def test_consent_and_waiting_keyboards() -> None:
    assert payloads(texts.consent_keyboard("ob:accept", "ob:decline")) == [
        "ob:policy",
        "ob:accept",
        "ob:decline",
    ]
    assert payloads(texts.consent_keyboard("ob:pconsent:7")) == ["ob:policy", "ob:pconsent:7"]
    assert payloads(texts.waiting_parent_keyboard()) == ["ob:resend", "ob:example"]
    assert payloads(texts.add_child_keyboard()) == ["ob:addchild"]


def test_whose_keyboard_numbers_children_of_same_grade() -> None:
    buttons = texts.whose_keyboard([profile(1, 2), profile(2, 4), profile(3, 2)])
    assert [b["text"] for row in buttons for b in row] == [
        "2 класс, ребёнок 1",
        "4 класс",
        "2 класс, ребёнок 2",
    ]
    assert payloads(buttons) == ["ob:whose:1", "ob:whose:2", "ob:whose:3"]


def test_parent_status_lists_children_and_waiting_links() -> None:
    status = texts.parent_status([profile(1, 2), profile(2, 7, own=True)], [8])
    assert status.splitlines()[:4] == [
        texts.PARENT_STATUS_HEADER,
        "• 2 класс — фото присылаете вы",
        "• 7 класс — свой MAX",
        "• 8 класс — ждём, когда ребёнок откроет ссылку",
    ]
    assert "пришлите фото" in status
    assert "пришлите фото" not in texts.parent_status([profile(2, 7, own=True)], [])


@pytest.mark.parametrize("kind", ["student_invites_parent", "parent_invites_student"])
@pytest.mark.parametrize("result", ["expired", "used", "invalid", "role_mismatch", "has_parent"])
def test_every_refusal_has_text(kind: str, result: str) -> None:
    assert texts.invite_refusal(kind, result) != "Ссылка не подошла."  # type: ignore[arg-type]


def test_code_texts_address_parent_and_child() -> None:
    assert "Проверьте" in texts.code_invalid(formal=True)
    assert "Проверь " in texts.code_invalid(formal=False)
    assert "Попробуйте" in texts.code_rate_limited(formal=True)
    assert "Попробуй " in texts.code_rate_limited(formal=False)
