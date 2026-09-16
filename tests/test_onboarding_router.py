"""Сценарии онбординга целиком через маршрутизатор (спецификация §4, §12, §15)."""

from pathlib import Path

from hwcheck.bot.fsm import ChatState
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import policy_messages
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding
from onboarding_kit import (
    Kit,
    actor,
    chat,
    code_of,
    link_token,
    make_kit,
    payloads,
    photo,
    press,
    ready_student,
    start,
    text,
)


def journey_types(kit: Kit, user_id: int) -> list[str]:
    user = actor(user_id).user_hash
    return [e["type"] for e in kit.events() if e["user"] == user and e["type"] != "button_pressed"]


async def test_student_journey_until_parent_consents(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    assert await ob.route(start(1)) == "handled"
    assert kit.last(1) == (texts.HELLO, texts.role_keyboard())
    await ob.route(press(1, "ob:role:student"))
    assert kit.last(1) == (texts.STUDENT_GRADE, texts.grade_keyboard("grade"))
    await ob.route(press(1, "ob:grade:7"))
    await ob.route(press(1, "ob:subject:history"))
    await ob.route(press(1, "ob:subject:math"))
    invite_message = kit.texts(1)[-1]
    assert await ob.route(photo(1, "https://files/1.jpg")) == "handled"
    assert kit.last(1)[0] == texts.PHOTO_BLOCKED
    assert await ob.route(text(1, "ну когда уже")) == "handled"
    assert kit.last(1)[0] == texts.WAITING_PARENT
    assert kit.max.callbacks == ["cb-ob:role:student", "cb-ob:grade:7"] + [
        "cb-ob:subject:history",
        "cb-ob:subject:math",
    ]

    assert await ob.route(start(2, f"p_{link_token(invite_message)}")) == "handled"
    accept = payloads(kit.last(2)[1])[1]  # «Согласен» с меткой показанной ссылки (ruling Task 7)
    assert accept.startswith("ob:accept:")
    await ob.route(press(2, accept))
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    assert kit.max.to_users[-1][0] == 1
    assert await ob.route(photo(1, "https://files/2.jpg")) == "pass"
    assert await ob.route(text(1, "4F7K-92QD")) == "pass"  # у ученика с родителем — просто текст

    assert journey_types(kit, 1) == [
        "bot_started",
        "onboarding_role_chosen",
        "onboarding_grade_chosen",
        "onboarding_subject_chosen",
        "subject_waitlist",
        "onboarding_subject_chosen",
        "invite_created",
        "photo_blocked_no_consent",
    ]
    assert journey_types(kit, 2) == [
        "bot_started",
        "invite_opened",
        "consent_given",
        "child_linked",
        "notify_sent",
    ]


async def test_young_student_and_foreign_buttons(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    await ob.route(press(1, "ob:grade:3"))  # кнопка из старого сообщения — шаг «роль» ещё идёт
    assert kit.texts(1)[-2] == texts.YOUNG_STUDENT
    assert await kit.repo.get_account(actor(1).user_hash) is None

    await ob.route(press(5, "ob:grade:7"))
    subject_step = (texts.SUBJECT_STUDENT, texts.subject_keyboard(7))
    for payload in ("ob:pgrade:3", "ob:whose:1", "ob:consent", "ob:bogus", "ob:grade:²"):
        assert await ob.route(press(5, payload)) == "handled"
        assert kit.last(5) == subject_step, payload
    assert await ob.route(press(5, "tutor:0")) == "handled"  # кнопка проверки до онбординга
    assert kit.max.callbacks[-1] == "cb-tutor:0"
    assert await ob.route(MaxUpdate.model_validate({"update_type": "chat_title_changed"})) == "pass"


async def test_parent_with_two_young_children(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    assert kit.last(2)[0] == texts.INSTRUCTION_PARENT
    assert await ob.route(photo(2, "u1")) == CheckPhotos(["u1"])

    for payload in ("ob:addchild", "ob:pgrade:4", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    assert await ob.route(photo(2, "u2")) == "handled"
    question, buttons = kit.last(2)
    assert question == texts.WHOSE_HOMEWORK
    assert await ob.route(press(2, payloads(buttons)[1])) == CheckPhotos(["u2"])
    assert await ob.route(photo(2, "u3")) == CheckPhotos(["u3"])

    assert await ob.route(text(2, "что дальше?")) == "handled"
    assert kit.last(2)[0].startswith(texts.PARENT_STATUS_HEADER)
    await kit.ctx.dialogs.set(chat(2), ChatState(phase="tutoring"))
    assert await ob.route(text(2, "получилось 12")) == "pass"  # родитель отвечает тьютору


async def test_parent_keeps_checking_consented_child_while_adding_another(
    tmp_path: Path,
) -> None:
    """Незавершённый второй ребёнок не должен запирать проверку уже подключённого (ревью Task 9)."""
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math"):
        await ob.route(press(2, payload))
    # пока нет ни одного согласия — фото блокируются, как и раньше
    assert await ob.route(photo(2, "before")) == "handled"
    assert kit.events("photo_blocked_no_consent") != []

    await ob.route(press(2, "ob:consent"))  # ребёнок 1 (2 класс) подключён
    for payload in ("ob:addchild", "ob:pgrade:3"):
        await ob.route(press(2, payload))  # ребёнок 2 (3 класс) заведён, но не завершён
    blocked_before = len(kit.events("photo_blocked_no_consent"))

    assert await ob.route(photo(2, "u")) == CheckPhotos(["u"])
    assert len(kit.events("photo_blocked_no_consent")) == blocked_before

    await kit.ctx.dialogs.set(chat(2), ChatState(phase="tutoring"))
    assert await ob.route(text(2, "получилось 5")) == "pass"
    assert await ob.route(press(2, "tutor:0")) == "pass"

    await kit.ctx.dialogs.set(chat(2), ChatState())
    assert await ob.route(text(2, "что дальше?")) == "handled"
    assert kit.last(2)[0] == texts.SUBJECT_PARENT  # шаг незавершённого ребёнка 3 класса


async def test_whose_homework_while_adding_third_child(tmp_path: Path) -> None:
    """«Чья домашка?» не застревает, пока заводится незавершённый ребёнок (финальное ревью, F2)."""
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    for payload in ("ob:addchild", "ob:pgrade:4", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    for payload in ("ob:addchild", "ob:pgrade:3"):
        await ob.route(press(2, payload))  # третий ребёнок заведён, но не завершён

    assert await ob.route(photo(2, "u1", "u2")) == "handled"
    question, buttons = kit.last(2)
    assert question == texts.WHOSE_HOMEWORK
    assert await ob.route(press(2, payloads(buttons)[0])) == CheckPhotos(["u1", "u2"])


async def test_code_like_answer_in_dialog_goes_to_check(tmp_path: Path) -> None:
    """Ответ тьютору, похожий на запасной код, не съедается онбордингом (финальное ревью, F3)."""
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    await kit.ctx.dialogs.set(chat(2), ChatState(phase="tutoring"))
    assert await ob.route(text(2, "23456789")) == "pass"
    assert kit.events("invite_opened") == []


async def test_group_chat_updates_are_swallowed(tmp_path: Path) -> None:
    """Ссылки, коды и экран согласия не уходят в групповой чат (финальное ревью, F4)."""
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    group_photo = photo(7, "u", chat_type="chat")
    group_press = press(7, "ob:role:student", chat_type="chat")
    assert (group_photo.chat_type, group_press.chat_type) == ("chat", "chat")
    assert (photo(7, "u").chat_type, press(7, "ob:role:student").chat_type) == (None, None)

    assert await ob.route(group_photo) == "handled"
    assert await ob.route(group_press) == "handled"
    assert kit.max.sent == [] and kit.max.to_users == []
    assert kit.max.callbacks == ["cb-ob:role:student"]
    assert await kit.repo.get_account(actor(7).user_hash) is None

    assert await ob.route(photo(7, "u", chat_type="dialog")) == "handled"
    assert kit.last(7)[0] == texts.HELLO  # в диалоге с ботом — как обычно


async def test_parent_first_then_child_joins_by_link(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:7"):
        await ob.route(press(2, payload))
    assert await kit.repo.get_account(actor(2).user_hash) is None
    await ob.route(press(2, "ob:pconsent:7"))
    child_message = kit.texts(2)[-1]

    assert await ob.route(start(3, f"c_{link_token(child_message)}")) == "handled"
    assert kit.texts(3)[-2:] == [texts.CHILD_LINKED, texts.SUBJECT_STUDENT]
    assert kit.max.to_users == [(2, texts.CHILD_JOINED.format(grade=7), None)]
    await ob.route(press(3, "ob:subject:math"))
    assert kit.last(3)[0] == texts.INSTRUCTION_STUDENT
    assert await ob.route(photo(3, "u")) == "pass"

    await ob.route(text(2, "как там ребёнок?"))
    assert "• 7 класс — свой MAX" in kit.last(2)[0]
    assert await ob.route(photo(2, "u")) == "handled"
    assert kit.last(2)[0] == texts.PARENT_PHOTO_NO_YOUNG


async def test_code_policy_example_and_resend(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:grade:8", "ob:subject:math"):
        await ob.route(press(1, payload))
    first_message = kit.texts(1)[-1]
    await ob.route(press(1, "ob:resend"))
    assert kit.texts(1)[-2] == texts.NEED_PARENT and kit.texts(1)[-1] != first_message
    await ob.route(press(1, "ob:example"))
    assert kit.last(1)[0] == texts.EXAMPLE

    assert await ob.route(text(2, code_of(first_message).lower())) == "handled"
    assert kit.last(2)[0] == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=8))
    decline = payloads(kit.last(2)[1])[2]  # «Отказать» с меткой показанной ссылки
    await ob.route(press(2, "ob:policy"))
    assert kit.texts(2)[-len(policy_messages()) :] == policy_messages()
    await ob.route(press(2, decline))
    assert kit.max.to_users[-1][1] == texts.PARENT_NOT_ALLOWED
    await ob.route(press(1, "ob:resend"))  # ребёнок отправляет ссылку ещё раз
    assert kit.texts(1)[-2] == texts.NEED_PARENT


async def test_start_with_broken_payload_and_ready_student_start(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    assert await ob.route(start(1, "p_!!")) == "handled"
    assert kit.last(1)[0] == texts.HELLO
    assert kit.events("bot_started")[0]["invite"] is False
    await ready_student(kit, user_id=4)
    assert await ob.route(start(4)) == "handled"
    assert kit.last(4)[0] == texts.INSTRUCTION_STUDENT
