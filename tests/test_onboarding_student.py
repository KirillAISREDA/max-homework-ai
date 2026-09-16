"""Ученик в своём MAX (спецификация §4.1, §4.7 п.1, §10.2): класс, предмет, ссылка родителю."""

from datetime import timedelta
from pathlib import Path

from hwcheck.bot.invites import digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from onboarding_kit import BOT, Kit, actor, code_of, link_token, make_kit, payloads


def steps(kit: Kit) -> tuple[SubjectStep, StudentSteps]:
    subjects = SubjectStep(kit.ctx)
    return subjects, StudentSteps(kit.ctx, subjects)


async def test_young_student_gets_bot_link_for_parent_and_nothing_is_stored(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    await students.choose_grade(actor(1), 3)
    link = texts.BOT_LINK_FOR_PARENT.format(link=f"https://max.ru/{BOT}")
    assert kit.texts(1) == [texts.YOUNG_STUDENT, link]
    assert await kit.repo.get_account(actor(1).user_hash) is None
    assert kit.events("onboarding_parent_required")[0]["grade"] == 3


async def test_student_grade_and_subject_with_waitlist(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    subjects, students = steps(kit)
    me = actor(1)
    await students.choose_grade(me, 7)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None and account.role == "student"
    profile = await kit.repo.own_profile(account.id)
    assert profile is not None and (profile.grade, profile.grade_year) == (7, 2026)
    question, buttons = kit.last(1)
    assert question == texts.SUBJECT_STUDENT and "ob:subject:physics" in payloads(buttons)
    assert kit.events("onboarding_grade_chosen")[0]["role"] == "student"

    assert not await subjects.choose(me, profile, "history")
    assert (me.user_hash, "history") in kit.repo.waitlist
    assert texts.SUBJECT_SOON.format(title="История") in kit.texts(1)
    assert not await subjects.choose(me, profile, "world_around")  # предмет 1–4 классов
    assert not await subjects.choose(me, profile, "../../etc")
    assert kit.last(1)[0] == texts.SUBJECT_STUDENT
    assert await subjects.choose(me, profile, "math")
    chosen = await kit.repo.own_profile(account.id)
    assert chosen is not None and chosen.subject == "math"
    events = kit.events("onboarding_subject_chosen")
    assert [(e["subject"], e["available"]) for e in events] == [
        ("history", False),
        ("math", True),
    ]


async def test_student_without_parent_gets_message_to_forward(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    me = actor(1)
    await students.choose_grade(me, 7)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None
    profile = await kit.repo.own_profile(account.id)
    assert profile is not None
    await students.after_subject(me, profile)

    need, message = kit.texts(1)[-2:]
    assert need == texts.NEED_PARENT
    assert f"https://max.ru/{BOT}?start=p_" in message
    invite = await kit.repo.find_invite(token_hash=digest(link_token(message)))
    assert invite is not None
    assert (invite.kind, invite.grade) == ("student_invites_parent", 7)
    assert invite.expires_at == kit.clock.now + timedelta(days=7)
    by_code = await kit.repo.find_invite(code_hash=digest(code_of(message).replace("-", "")))
    assert by_code == invite
    assert kit.events("invite_created")[0]["kind"] == "student_invites_parent"


async def test_waiting_student_reminded_and_photo_blocked(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    await students.remind_waiting(actor(1))
    assert kit.last(1)[0] == texts.WAITING_PARENT
    await students.block_photo(actor(1))
    blocked, buttons = kit.last(1)
    assert blocked == texts.PHOTO_BLOCKED and payloads(buttons) == ["ob:resend", "ob:example"]
