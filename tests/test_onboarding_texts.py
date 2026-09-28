"""Тексты и клавиатуры онбординга, политика сообщениями MAX (спецификация §4, §10.3)."""

import re

import pytest

from hwcheck.bot.max_api import Buttons
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import (
    MAX_MESSAGE_LEN,
    POLICY_VERSION,
    policy_messages,
    split_message,
)
from hwcheck.bot.subjects import SUBJECTS
from hwcheck.db.repo import StudentProfile

INTRO_LIMIT = 450  # вводное заметно короче предела MAX: его читают с телефона, до первого вопроса
# Положение конкурса, п. 5.4: призы, розыгрыши, подарки и бонусы за активность обещать нельзя
FORBIDDEN = re.compile(r"\b(приз(а|у|ом|е|ы|ов|ам|ами|ах)?|розыгрыш\w*|подар\w*|бонус\w*)\b")


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


def all_texts() -> list[str]:
    """Все строки texts.py: константы, отказы по ссылкам и тексты, которые собирают функции."""
    found = [value for value in vars(texts).values() if isinstance(value, str)]
    for value in vars(texts).values():
        if isinstance(value, dict):
            found += [item for item in value.values() if isinstance(item, str)]
    for formal in (True, False):
        found += [texts.code_invalid(formal), texts.code_rate_limited(formal)]
    found.append(texts.parent_status([profile(1, 2), profile(2, 7, own=True)], [8]))
    return found


def test_hello_is_short_fork_that_says_what_domashka_is() -> None:
    assert "Домашка" in texts.HELLO and "фото" in texts.HELLO and "ошибк" in texts.HELLO
    assert texts.HELLO.endswith("Кто ты?") and len(texts.HELLO) <= 120


def test_student_intro_says_where_he_is_and_what_bot_does() -> None:
    intro = texts.STUDENT_INTRO
    assert intro.startswith("Ты в Домашке")
    for meaning in ("тетрадь", "за минуту", "где ошибка", "не подсказываю", "самому"):
        assert meaning in intro, meaning
    assert "пара вопросов" in intro and "разрешение родителя" in intro  # что дальше
    assert not re.search(r"\b[Вв](ы|ам|ас|аш\w*)\b", intro)  # с учеником — на «ты»
    welcome = texts.STUDENT_WELCOME
    assert welcome == f"{intro}\n\n{texts.STUDENT_GRADE}" and len(welcome) <= INTRO_LIMIT


def test_parent_intro_says_where_he_is_and_what_bot_does() -> None:
    intro = texts.PARENT_INTRO
    assert intro.startswith("Вы в Домашке")
    for meaning in (
        "Вам не нужно каждый вечер проверять домашку и вспоминать школьную программу",
        "за минуту находит ошибки",
        "Готовых ответов бот не даёт",
        "сам нашёл и исправил ошибку",
        "Вычисления пересчитывает программа, а не нейросеть",
    ):
        assert meaning in intro, meaning
    assert "класс ребёнка" in intro and "согласие на обработку данных" in intro  # что дальше
    assert not re.search(r"\b(ты|тебя|тебе)\b", intro.lower())  # с родителем — на «вы»
    welcome = texts.PARENT_WELCOME
    assert welcome == f"{intro}\n\n{texts.PARENT_GRADE}" and len(welcome) <= INTRO_LIMIT


def test_summary_is_not_promised_before_notifications_exist() -> None:
    """Итог родителю после проверки обещают только тексты ветки уведомлений (§9.1): обещание в
    экране согласия без работающей рассылки — ложное. За 1–4 класс фото присылает сам родитель
    и результат видит сразу, в том же чате."""
    for text in (
        texts.PARENT_INTRO,
        texts.CHILD_ASKS_CONSENT,
        texts.PARENT_FIRST_CONSENT,
        texts.PARENT_SENDS_CONSENT,
    ):
        assert "итог" not in text
    assert "фото домашки присылаете вы" in texts.PARENT_SENDS_CONSENT
    assert "сразу" in texts.PARENT_SENDS_CONSENT


def test_linked_sides_learn_what_domashka_is() -> None:
    child = texts.CHILD_LINKED
    assert child.startswith("Привет! Родитель подключил тебя к Домашке")
    for meaning in ("бот", "фото", "за минуту", "где ошибка", "не подсказываю", "самому"):
        assert meaning in child, meaning
    assert "Разрешение родителя уже есть" in child and len(child) <= INTRO_LIMIT

    parent = texts.CHILD_ASKS_CONSENT.format(grade=7)
    for meaning in ("бот", "фото", "находит ошибки", "без готовых ответов", "не нейросеть"):
        assert meaning in parent, meaning
    assert "согласие" in parent and len(parent) <= INTRO_LIMIT

    young = texts.YOUNG_STUDENT
    assert "мама или папа" in young and "ссылку" in young
    assert "фото" in texts.BOT_LINK_FOR_PARENT and "{link}" in texts.BOT_LINK_FOR_PARENT


def test_no_prizes_raffles_gifts_or_bonuses_are_promised() -> None:
    assert FORBIDDEN.search("выиграй приз") and FORBIDDEN.search("розыгрыши и подарки, бонусы")
    assert not FORBIDDEN.search("признак делимости")
    found = all_texts()
    assert texts.STUDENT_WELCOME in found
    assert texts.invite_refusal("parent_invites_student", "expired") in found
    for value in [*found, *policy_messages()]:
        assert not FORBIDDEN.search(value.lower()), value


def test_ready_answers_are_mentioned_only_as_refused() -> None:
    """Готовые ответы бот не даёт: упоминать их можно только с отрицанием."""
    mention = re.compile(r"[^.!?\n]*готов\w+ ответ\w+[^.!?\n]*")
    mentions = [m.group().lower() for value in all_texts() for m in mention.finditer(value)]
    assert len(mentions) >= 4  # оба вводных, инструкции, приглашение родителю
    for sentence in mentions:
        assert re.search(r"\b(не|без)\b", sentence), sentence


def test_intros_name_only_the_subject_that_works() -> None:
    """Сейчас проверяется только математика: остальные предметы вводные не называют."""
    intros = [
        texts.HELLO,
        texts.STUDENT_INTRO,
        texts.PARENT_INTRO,
        texts.CHILD_LINKED,
        texts.CHILD_ASKS_CONSENT,
        texts.YOUNG_STUDENT,
        texts.BOT_LINK_FOR_PARENT,
    ]
    stems = [subject.title[:5].lower() for subject in SUBJECTS if not subject.available]
    assert "русск" in stems
    for intro in intros:
        assert not [stem for stem in stems if stem in intro.lower()], intro
    for intro in (texts.STUDENT_INTRO, texts.PARENT_INTRO, texts.CHILD_LINKED):
        assert "математик" in intro


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
    intros = [
        texts.CHILD_ASKS_CONSENT.format(grade=9),
        texts.PARENT_FIRST_CONSENT.format(grade=9),
        texts.PARENT_SENDS_CONSENT,
    ]
    assert all(len(texts.consent_text(intro)) <= MAX_MESSAGE_LEN for intro in intros)


def test_consent_summary_promises_only_what_exists() -> None:
    """Меню отзыва появится на этапе 3 — до него отзыв через оператора (финальное ревью, F10)."""
    lines = texts.CONSENT_SUMMARY.splitlines()
    assert "меню" not in texts.CONSENT_SUMMARY
    assert "Как отозвать: написать оператору — контакт в полном тексте." in lines
    assert lines[-2:] == [
        "Нажимая «Согласен», вы подтверждаете, что вы родитель или законный представитель ребёнка.",
        f"Полный текст — кнопка «Полный текст» (политика {POLICY_VERSION}).",
    ]


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
