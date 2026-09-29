"""Обновление согласия: семья, согласившаяся по прежней политике, получает новое согласие — единое.

Политика v2 называет обоих получателей: фото страниц читает Gemini, текст заданий проверяет
GigaChat. Пока родитель не согласился заново, ребёнок не ждёт: фото читает прежняя модель.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.onboarding.renewal import REMIND_EVERY
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding
from hwcheck.db.repo import ProfileRepository
from onboarding_kit import Kit, actor, make_kit, photo, press, ready_student
from test_profile_repo import NOW, repo  # noqa: F401 — фикстура обеих реализаций

CHILD, PARENT, STRANGER = 1, 101, 7


async def young_child(kit: Kit, policy: str, grade: int = 3) -> int:
    """Ребёнок 1–4 класса: фото присылает родитель из своего чата."""
    me = actor(PARENT)
    parent = await kit.repo.get_or_create_account(me.user_hash, kit.ctx.encrypted_id(me), "parent")
    child = await kit.repo.start_child_by_parent(parent.id, grade, 2026)
    await kit.repo.set_subject(child.id, "math")
    assert await kit.repo.give_parent_consent(child.id, me.user_hash, policy, kit.clock.now)
    return child.id


def renewal_button(profile_id: int) -> str:
    return f"ob:renew:{profile_id}"


# --- хранилище ---


async def test_renewal_replaces_consent_and_keeps_the_record(repo: ProfileRepository) -> None:  # noqa: F811
    parent = await repo.get_or_create_account("p1", b"p", "parent")
    child = await repo.start_child_by_parent(parent.id, 3, 2026)
    assert not await repo.renew_consent(child.id, parent.id, "p1", "v2", NOW)  # согласия ещё нет
    assert await repo.give_parent_consent(child.id, "p1", "v0", NOW)

    assert await repo.renew_consent(child.id, parent.id, "p1", "v2", NOW)
    renewed = await repo.get_profile(child.id)
    assert renewed is not None and (renewed.has_consent, renewed.consent_policy) == (True, "v2")
    # повторное нажатие: согласие уже по этой версии
    assert not await repo.renew_consent(child.id, parent.id, "p1", "v2", NOW)


async def test_renewal_is_only_for_parent_of_the_child(repo: ProfileRepository) -> None:  # noqa: F811
    parent = await repo.get_or_create_account("p1", b"p", "parent")
    other = await repo.get_or_create_account("p2", b"q", "parent")
    child = await repo.start_child_by_parent(parent.id, 3, 2026)
    assert await repo.give_parent_consent(child.id, "p1", "v0", NOW)

    assert not await repo.renew_consent(child.id, other.id, "p2", "v2", NOW)
    assert not await repo.renew_consent(child.id + 100, parent.id, "p1", "v2", NOW)
    unchanged = await repo.get_profile(child.id)
    assert unchanged is not None and unchanged.consent_policy == "v0"


# --- родитель присылает фото сам (1–4 класс) ---


async def test_parent_with_old_consent_is_asked_once_and_check_goes_on(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    child = await young_child(kit, "v1")
    ob = Onboarding(kit.ctx)

    route = await ob.route(photo(PARENT, "u1"))
    # проверка не ждёт согласия: фото читает прежняя модель
    assert route == CheckPhotos(["u1"], subject="math", student_id=child, policy_version="v1")
    message, buttons = kit.last(PARENT)
    assert message == texts.renewal_text("Ребёнок (3 класс)")
    assert "Gemini" in message and "GigaChat" in message and "за пределы России" in message
    assert [b["payload"] for row in buttons for b in row] == ["ob:policy", renewal_button(child)]
    [asked] = kit.events("consent_renewal_asked")
    assert (asked["from_policy"], asked["policy_version"]) == ("v1", POLICY_VERSION)

    sent = len(kit.texts(PARENT))
    await ob.route(photo(PARENT, "u2"))  # в тот же день не напоминаем
    assert len(kit.texts(PARENT)) == sent

    kit.clock.now += REMIND_EVERY + timedelta(minutes=1)
    await ob.route(photo(PARENT, "u3"))
    assert kit.texts(PARENT)[-1] == texts.renewal_text("Ребёнок (3 класс)")


async def test_parent_agrees_and_next_photo_goes_by_new_consent(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    child = await young_child(kit, "v0")
    ob = Onboarding(kit.ctx)
    await ob.route(photo(PARENT, "u1"))

    assert await ob.route(press(PARENT, renewal_button(child))) == "handled"
    assert kit.last(PARENT)[0] == texts.RENEWAL_THANKS
    [renewed] = kit.events("consent_renewed")
    assert (renewed["from_policy"], renewed["policy_version"]) == ("v0", POLICY_VERSION)

    route = await ob.route(photo(PARENT, "u2"))
    assert route == CheckPhotos(
        ["u2"], subject="math", student_id=child, policy_version=POLICY_VERSION
    )
    assert not kit.events("consent_renewal_asked")[1:]  # больше не спрашиваем

    # повторное нажатие старой кнопки ничего не ломает и не плодит записей
    assert await ob.route(press(PARENT, renewal_button(child))) == "handled"
    assert len(kit.events("consent_renewed")) == 1


async def test_consent_by_current_policy_is_not_asked_again(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    await young_child(kit, POLICY_VERSION)
    await Onboarding(kit.ctx).route(photo(PARENT, "u1"))
    assert kit.events("consent_renewal_asked") == []


# --- ребёнок 5–9 класса со своим аккаунтом: согласие даёт родитель из своего чата ---


async def test_parent_of_older_child_is_asked_in_own_chat(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile = await ready_student(kit, policy="v1")
    ob = Onboarding(kit.ctx)

    route = await ob.route(photo(CHILD, "u1"))
    assert route == CheckPhotos(["u1"], subject="math", student_id=profile.id, policy_version="v1")
    assert kit.texts(CHILD) == []  # ребёнка вопрос о согласии не касается
    [(to, message, buttons)] = kit.max.to_users
    assert to == PARENT and message == texts.renewal_text("Ребёнок (7 класс)")
    assert [b["payload"] for row in buttons for b in row][-1] == renewal_button(profile.id)

    assert await ob.route(press(PARENT, renewal_button(profile.id))) == "handled"
    route = await ob.route(photo(CHILD, "u2"))
    assert isinstance(route, CheckPhotos) and route.policy_version == POLICY_VERSION
    assert len(kit.max.to_users) == 1  # после согласия родителя больше не беспокоим


@pytest.mark.parametrize("who", [CHILD, STRANGER])
async def test_only_parent_can_renew(tmp_path: Path, who: int) -> None:
    """Кнопку могли переслать или подделать: согласие меняет только родитель этого ребёнка."""
    kit = make_kit(tmp_path)
    profile = await ready_student(kit, policy="v1")
    ob = Onboarding(kit.ctx)

    await ob.route(press(who, renewal_button(profile.id)))
    await ob.route(press(PARENT, "ob:renew:не-число"))
    await ob.route(press(PARENT, renewal_button(profile.id + 100)))

    unchanged = await kit.repo.get_profile(profile.id)
    assert unchanged is not None and unchanged.consent_policy == "v1"
    assert kit.events("consent_renewed") == []


async def test_blocked_parent_does_not_stop_the_check(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile = await ready_student(kit, policy="v1")
    kit.max.blocked_users = {PARENT}
    route = await Onboarding(kit.ctx).route(photo(CHILD, "u1"))
    assert isinstance(route, CheckPhotos) and route.student_id == profile.id
    [failed] = kit.events("notify_failed")
    assert failed["kind"] == "consent_renewal"
