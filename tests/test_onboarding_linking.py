"""Связка «ребёнок ↔ родитель» по ссылке и коду (спецификация §4.2, §4.3 п.6, §4.4, §11)."""

from datetime import timedelta
from pathlib import Path

from hwcheck.bot.invites import new_invite
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.linking import Linking
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import StudentProfile
from onboarding_kit import Kit, actor, code_of, link_token, make_kit, payloads


def linking(kit: Kit) -> Linking:
    return Linking(kit.ctx, SubjectStep(kit.ctx))


def consent_tag(buttons: Buttons | None) -> str:
    """Тег приглашения из payload кнопки «Согласен» (`ob:accept:<tag>`)."""
    return payloads(buttons)[1].removeprefix("ob:accept:")


async def waiting_student(kit: Kit, user_id: int = 1) -> tuple[StudentProfile, str]:
    """Ученик 7 класса с предметом и ссылкой родителю: (профиль, сообщение для пересылки)."""
    me = actor(user_id)
    profile = await kit.repo.create_student(me.user_hash, kit.ctx.encrypted_id(me), 7, 2026)
    assert profile is not None
    await kit.repo.set_subject(profile.id, "math")
    return profile, await forward_again(kit, profile, user_id)


async def forward_again(kit: Kit, profile: StudentProfile, user_id: int = 1) -> str:
    await StudentSteps(kit.ctx, SubjectStep(kit.ctx)).invite_parent(actor(user_id), profile)
    return kit.texts(user_id)[-1]


async def test_parent_opens_link_and_consents(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile, message = await waiting_student(kit)
    parent = actor(2)
    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    tag = consent_tag(buttons)
    assert len(tag) == 12
    assert payloads(buttons) == ["ob:policy", f"ob:accept:{tag}", f"ob:decline:{tag}"]

    await linking(kit).accept(parent, tag)
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
    assert kit.max.to_users == [(1, allowed, None)]
    assert profile.user_id is not None
    linked = await kit.repo.own_profile(profile.user_id)
    assert linked is not None and linked.has_consent
    assert [e["scenario"] for e in kit.events("consent_given")] == ["child_link"]
    assert [e["result"] for e in kit.events("invite_opened")] == ["ok"]
    assert kit.events("notify_sent")[0]["kind"] == "consent_given"

    await linking(kit).accept(parent, tag)  # повторное «Согласен»
    assert kit.last(2)[0] == texts.LINK_GONE
    assert len(kit.max.to_users) == 1


async def test_parent_declines_and_child_can_resend(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    parent = actor(2)
    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    tag = consent_tag(kit.last(2)[1])
    await linking(kit).decline(parent, tag)
    assert kit.last(2)[0] == texts.DECLINED
    [(child_id, notice, buttons)] = kit.max.to_users
    assert (child_id, notice) == (1, texts.PARENT_NOT_ALLOWED)
    assert "ob:resend" in payloads(buttons)
    assert await kit.repo.get_account(parent.user_hash) is None
    assert len(kit.events("consent_declined")) == 1

    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    assert kit.last(2)[0] == texts.invite_refusal("student_invites_parent", "used")


async def test_child_opens_parent_link_and_parent_is_told(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    parent = actor(2)
    account = await kit.repo.get_or_create_account(
        parent.user_hash, kit.ctx.encrypted_id(parent), "parent"
    )
    invite = new_invite("parent_invites_student")
    await kit.repo.create_invite(
        invite,
        account.id,
        kit.clock.now + timedelta(days=7),
        grade=6,
        policy_version="v0",
        consent_at=kit.clock.now,
    )
    await linking(kit).open_link(actor(3), None, "parent_invites_student", invite.token)
    assert kit.texts(3) == [texts.CHILD_LINKED, texts.SUBJECT_STUDENT]
    assert kit.max.to_users == [(2, texts.CHILD_JOINED.format(grade=6), None)]
    assert kit.events("child_linked")[0]["grade"] == 6


async def test_link_refusals(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    token = link_token(message)
    student = await kit.repo.get_account(actor(1).user_hash)
    await linking(kit).open_link(actor(1), student, "student_invites_parent", token)
    assert kit.last(1)[0] == texts.invite_refusal("student_invites_parent", "role_mismatch")
    await linking(kit).open_link(actor(2), None, "parent_invites_student", token)  # не тот вид
    assert kit.last(2)[0] == texts.invite_refusal("parent_invites_student", "invalid")
    kit.clock.now += timedelta(days=8)
    await linking(kit).open_link(actor(2), None, "student_invites_parent", token)
    assert kit.last(2)[0] == texts.invite_refusal("student_invites_parent", "expired")
    results = [e["result"] for e in kit.events("invite_opened")]
    assert results == ["role_mismatch", "invalid", "expired"]


async def test_second_parent_is_refused(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile, first = await waiting_student(kit)
    second = await forward_again(kit, profile)  # ребёнок отправил ссылку второму родителю
    await linking(kit).open_link(actor(2), None, "student_invites_parent", link_token(first))
    await linking(kit).accept(actor(2), consent_tag(kit.last(2)[1]))
    await linking(kit).open_link(actor(4), None, "student_invites_parent", link_token(second))
    assert kit.last(4)[0] == texts.invite_refusal("student_invites_parent", "has_parent")


async def test_accept_with_wrong_tag_does_not_touch_other_invite(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile_a, message_a = await waiting_student(kit, user_id=1)
    profile_b, message_b = await waiting_student(kit, user_id=3)
    parent = actor(2)

    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message_a))
    tag_a = consent_tag(kit.last(2)[1])
    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message_b))
    tag_b = consent_tag(kit.last(2)[1])
    assert tag_a != tag_b

    await linking(kit).accept(parent, tag_a)  # старая ссылка A, показанная раньше и прокрученная
    assert kit.last(2)[0] == texts.LINK_GONE
    assert kit.max.to_users == []
    assert kit.events("consent_given") == []
    assert profile_a.user_id is not None
    linked_a = await kit.repo.own_profile(profile_a.user_id)
    assert linked_a is not None and not linked_a.has_consent

    await linking(kit).accept(parent, tag_b)
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    assert profile_b.user_id is not None
    linked_b = await kit.repo.own_profile(profile_b.user_id)
    assert linked_b is not None and linked_b.has_consent
    allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
    assert kit.max.to_users == [(3, allowed, None)]


async def test_late_decline_after_other_parent_accepted(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile, first = await waiting_student(kit)
    second = await forward_again(kit, profile)  # ребёнок отправил ссылку второму родителю
    mum, dad = actor(2), actor(4)

    await linking(kit).open_link(mum, None, "student_invites_parent", link_token(first))
    mum_tag = consent_tag(kit.last(2)[1])
    await linking(kit).open_link(dad, None, "student_invites_parent", link_token(second))
    dad_tag = consent_tag(kit.last(4)[1])

    await linking(kit).accept(dad, dad_tag)
    assert kit.last(4)[0] == texts.CONSENT_THANKS

    await linking(kit).decline(mum, mum_tag)
    assert kit.last(2)[0] == texts.invite_refusal("student_invites_parent", "has_parent")
    assert kit.events("consent_declined") == []
    allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
    assert kit.max.to_users == [(1, allowed, None)]  # ребёнку — ровно одно сообщение


async def test_code_opens_invite_with_attempt_limit(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    code = code_of(message).replace("-", "")
    parent = actor(2)
    for _ in range(5):
        await linking(kit).open_code(parent, None, "AAAAAAAA")
    assert kit.last(2)[0] == texts.code_invalid(formal=True)
    await linking(kit).open_code(parent, None, code)
    assert kit.last(2)[0] == texts.code_rate_limited(formal=True)
    kit.clock.now += timedelta(hours=1)
    await linking(kit).open_code(parent, None, code)
    assert kit.last(2)[0] == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    results = [(e["via"], e["result"]) for e in kit.events("invite_opened")]
    assert results == [("code", "invalid")] * 5 + [("code", "rate_limited"), ("code", "ok")]


async def test_blocked_child_does_not_break_consent(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    kit.max.blocked_users.add(1)
    await linking(kit).open_link(actor(2), None, "student_invites_parent", link_token(message))
    await linking(kit).accept(actor(2), consent_tag(kit.last(2)[1]))
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    [failed] = kit.events("notify_failed")
    assert (failed["kind"], failed["component"], failed["error"]) == (
        "consent_given",
        "notifier",
        "RuntimeError",
    )
