"""Родитель (спецификация §4.3, §4.7): дети 1–4 класса с фото от родителя, ссылка ребёнку
5–9 класса, список детей, «Чья домашка?»."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from hwcheck.bot.invites import digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.parent import OWNER_TTL_S, ParentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import Account, StudentProfile
from onboarding_kit import BOT, Kit, actor, link_token, make_kit, payloads


def parents(kit: Kit) -> ParentSteps:
    return ParentSteps(kit.ctx, SubjectStep(kit.ctx))


async def young_child(kit: Kit, grade: int, user_id: int = 2) -> tuple[Account, StudentProfile]:
    """Родитель добавил ребёнка 1–4 класса, выбрал математику и дал согласие."""
    me = actor(user_id)
    await parents(kit).choose_grade(me, await kit.repo.get_account(me.user_hash), grade)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None
    child = (await kit.repo.children(account.id))[-1]
    await kit.repo.set_subject(child.id, "math")
    await parents(kit).give_consent(me, replace(child, subject="math"))
    [consented] = [c for c in await kit.repo.children(account.id) if c.id == child.id]
    return account, consented


async def test_parent_of_young_child_sends_photos_himself(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    await parents(kit).choose_grade(me, None, 3)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None and account.role == "parent"
    [child] = await kit.repo.children(account.id)
    assert child.sent_by_parent and (child.grade, child.has_consent) == (3, False)
    question, buttons = kit.last(2)
    assert question == texts.SUBJECT_PARENT and "ob:subject:world_around" in payloads(buttons)

    await parents(kit).ask_consent(me)
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.PARENT_SENDS_CONSENT)
    assert payloads(buttons) == ["ob:policy", "ob:consent"]
    await parents(kit).give_consent(me, child)
    await parents(kit).give_consent(me, child)  # второе «Согласен»
    assert kit.last(2) == (texts.INSTRUCTION_PARENT, texts.add_child_keyboard())
    assert [e["scenario"] for e in kit.events("consent_given")] == ["parent_sends"]
    [consented] = await kit.repo.children(account.id)
    assert consented.has_consent


async def test_parent_of_older_child_consents_then_forwards_link(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    await parents(kit).choose_grade(me, None, 7)
    assert await kit.repo.get_account(me.user_hash) is None  # до «Согласен» ничего не хранится
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.PARENT_FIRST_CONSENT.format(grade=7))
    assert payloads(buttons) == ["ob:policy", "ob:pconsent:7"]

    await parents(kit).invite_child(me, None, 7)
    forward, message = kit.texts(2)[-2:]
    assert forward == texts.FORWARD_TO_CHILD
    assert f"https://max.ru/{BOT}?start=c_" in message
    invite = await kit.repo.find_invite(token_hash=digest(link_token(message)))
    assert invite is not None and (invite.kind, invite.grade) == ("parent_invites_student", 7)
    assert [e["scenario"] for e in kit.events("consent_given")] == ["parent_first"]


async def test_status_lists_children_and_waiting_links(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    account, child = await young_child(kit, 2)
    await parents(kit).invite_child(actor(2), account, 7)
    await parents(kit).show_status(actor(2), account)
    assert kit.last(2) == (texts.parent_status([child], [7]), texts.add_child_keyboard())

    lonely = await kit.repo.get_or_create_account("p9", b"x", "parent")
    await parents(kit).show_status(actor(9), lonely)
    assert kit.last(9) == (texts.PARENT_GRADE, texts.grade_keyboard("pgrade"))


async def test_photo_goes_to_check_with_one_young_child(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    older = await kit.repo.get_or_create_account(actor(3).user_hash, b"x", "parent")
    assert await parents(kit).on_photo(actor(3), older, ["u0"]) is None
    assert kit.last(3) == (texts.PARENT_PHOTO_NO_YOUNG, texts.add_child_keyboard())

    account, _ = await young_child(kit, 2)
    assert await parents(kit).on_photo(actor(2), account, ["u1"]) == ["u1"]


async def test_two_young_children_ask_whose_homework(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    account, second_grade = await young_child(kit, 2)
    _, fourth_grade = await young_child(kit, 4)
    steps = parents(kit)

    assert await steps.on_photo(me, account, ["u1"]) is None
    question, buttons = kit.last(2)
    assert question == texts.WHOSE_HOMEWORK
    assert payloads(buttons) == [f"ob:whose:{second_grade.id}", f"ob:whose:{fourth_grade.id}"]
    sent = len(kit.max.sent)
    assert await steps.on_photo(me, account, ["u2"]) is None  # тетрадь вслед за учебником
    assert len(kit.max.sent) == sent  # второй раз не спрашиваем
    assert kit.events("homework_owner_asked")[0]["n_children"] == 2

    assert await steps.choose_owner(me, account, 999) is None  # чужой профиль
    assert await steps.choose_owner(me, account, fourth_grade.id) == ["u1", "u2"]
    assert await steps.on_photo(me, account, ["u3"]) == ["u3"]  # выбор действует час

    kit.clock.now += timedelta(seconds=OWNER_TTL_S)
    assert await steps.on_photo(me, account, ["u4"]) is None
    assert kit.last(2)[0] == texts.WHOSE_HOMEWORK
    kit.clock.now += timedelta(seconds=OWNER_TTL_S)
    assert await steps.choose_owner(me, account, second_grade.id) is None  # фото устарели
    assert kit.last(2)[0] == texts.PHOTOS_EXPIRED
