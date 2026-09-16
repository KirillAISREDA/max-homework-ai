"""Контракт хранилища профилей (спецификация онбординга §4.4, §4.7, §6, §11).

Одни тесты для InMemoryProfileRepository и PgProfileRepository (Task 3): фейк, на котором
проверяются сценарии бота, ведёт себя как PostgreSQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from hwcheck.bot.invites import new_invite
from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.pool import create_pool
from hwcheck.db.repo import PgProfileRepository, ProfileRepository, StudentProfile

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
WEEK = timedelta(days=7)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[ProfileRepository]:
    if request.param == "memory":
        yield InMemoryProfileRepository()
        return
    if TEST_DATABASE_URL is None:
        if "CI" in os.environ:
            pytest.fail("в CI нужен TEST_DATABASE_URL")
        pytest.skip("нужен TEST_DATABASE_URL (PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await create_pool(TEST_DATABASE_URL, server_settings={"search_path": schema})
    try:
        yield PgProfileRepository(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def student(repo: ProfileRepository, name: str = "s1", grade: int = 7) -> StudentProfile:
    profile = await repo.create_student(name, name.encode(), grade, 2026)
    assert profile is not None
    return profile


async def parent_invite(repo: ProfileRepository, profile: StudentProfile) -> str:
    """Ссылка родителю от ученика; возвращает token_hash."""
    assert profile.user_id is not None
    invite = new_invite("student_invites_parent")
    await repo.create_invite(invite, profile.user_id, NOW + WEEK)
    return invite.token_hash


async def child_invite(
    repo: ProfileRepository, parent_hash: str = "p1", grade: int = 7
) -> tuple[str, str]:
    """Ссылка ребёнку от родителя; возвращает (token_hash, code_hash)."""
    parent = await repo.get_or_create_account(parent_hash, b"p", "parent")
    invite = new_invite("parent_invites_student")
    await repo.create_invite(
        invite, parent.id, NOW + WEEK, grade=grade, policy_version="v0", consent_at=NOW
    )
    return invite.token_hash, invite.code_hash


async def test_student_account_and_profile_created_once(repo: ProfileRepository) -> None:
    profile = await student(repo)
    assert (profile.grade, profile.grade_year, profile.subject) == (7, 2026, None)
    assert not profile.has_consent and not profile.sent_by_parent
    assert await repo.create_student("s1", b"s1", 5, 2026) is None  # повторное нажатие класса
    account = await repo.get_account("s1")
    assert account is not None and (account.role, account.user_id_enc) == ("student", b"s1")
    await repo.set_subject(profile.id, "math")
    own = await repo.own_profile(account.id)
    assert own is not None and own.subject == "math"


async def test_get_or_create_keeps_existing_role(repo: ProfileRepository) -> None:
    await student(repo)
    account = await repo.get_or_create_account("s1", b"other", "parent")
    assert (account.role, account.user_id_enc) == ("student", b"s1")
    assert await repo.get_account("nobody") is None


async def test_young_children_replace_unfinished_and_keep_consented(
    repo: ProfileRepository,
) -> None:
    parent = await repo.get_or_create_account("p1", b"p", "parent")
    first = await repo.start_child_by_parent(parent.id, 2, 2026)
    assert first.sent_by_parent and first.parent_user_id == parent.id
    replaced = await repo.start_child_by_parent(parent.id, 3, 2026)  # передумал с классом
    assert [c.grade for c in await repo.children(parent.id)] == [3]
    assert await repo.give_parent_consent(replaced.id, "p1", "v0", NOW)
    assert not await repo.give_parent_consent(replaced.id, "p1", "v0", NOW)  # второе «Согласен»
    second = await repo.start_child_by_parent(parent.id, 4, 2026)
    children = await repo.children(parent.id)
    assert [(c.grade, c.has_consent) for c in children] == [(3, True), (4, False)]
    assert children[1].id == second.id


async def test_waitlist_ignores_duplicates(repo: ProfileRepository) -> None:
    await repo.add_to_waitlist("s1", "history", 7)
    await repo.add_to_waitlist("s1", "history", 7)


async def test_parent_accepts_student_invite_once(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    found = await repo.find_invite(token_hash=token)
    assert found is not None
    assert (found.kind, found.grade, found.used) == ("student_invites_parent", 7, False)

    outcome = await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"s1")
    assert outcome.profile is not None and outcome.profile.has_consent
    parent = await repo.get_account("p1")
    assert parent is not None and parent.role == "parent"
    assert [c.id for c in await repo.children(parent.id)] == [profile.id]
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "used"
    used = await repo.find_invite(token_hash=token)
    assert used is not None and used.used


async def test_parent_invite_refusals(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    second = await parent_invite(repo, profile)  # ребёнок отправил ссылку двоим
    expired = await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW + WEEK)
    assert expired.result == "expired"
    await student(repo, "s2")
    assert (
        await repo.accept_parent_invite(token, "s2", b"s2", "v0", NOW)
    ).result == "role_mismatch"
    assert (await repo.accept_parent_invite("nope", "p1", b"p1", "v0", NOW)).result == "invalid"
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "ok"
    assert (await repo.accept_parent_invite(second, "p2", b"p2", "v0", NOW)).result == "has_parent"
    assert await repo.get_account("p2") is None


async def test_decline_burns_invite_without_storing_parent(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    outcome = await repo.decline_parent_invite(token, NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"s1")
    assert (await repo.decline_parent_invite(token, NOW)).result == "used"
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "used"
    assert await repo.get_account("p1") is None


async def test_child_opens_parent_invite(repo: ProfileRepository) -> None:
    token, code_hash = await child_invite(repo)
    found = await repo.find_invite(code_hash=code_hash)
    assert found is not None and (found.token_hash, found.grade) == (token, 7)
    parent = await repo.get_account("p1")
    assert parent is not None
    assert [i.token_hash for i in await repo.open_child_invites(parent.id, NOW)] == [token]

    outcome = await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"p")
    assert outcome.profile is not None
    linked = outcome.profile
    assert (linked.grade, linked.parent_user_id, linked.has_consent) == (7, parent.id, True)
    assert await repo.open_child_invites(parent.id, NOW) == []
    assert (await repo.accept_child_invite(token, "c2", b"c2", 2026, NOW)).result == "used"


async def test_child_invite_for_registered_student_keeps_own_grade(
    repo: ProfileRepository,
) -> None:
    await student(repo, "s1", grade=8)
    token, _ = await child_invite(repo, grade=7)
    outcome = await repo.accept_child_invite(token, "s1", b"s1", 2026, NOW)
    assert outcome.result == "ok" and outcome.profile is not None
    assert outcome.profile.grade == 8


async def test_child_invite_refusals(repo: ProfileRepository) -> None:
    token, _ = await child_invite(repo, "p1")
    assert (await repo.accept_child_invite(token, "p1", b"p", 2026, NOW)).result == "role_mismatch"
    assert (await repo.accept_parent_invite(token, "x", b"x", "v0", NOW)).result == "invalid"
    late = await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW + WEEK)
    assert late.result == "expired"
    assert (await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW)).result == "ok"
    other, _ = await child_invite(repo, "p2")
    assert (await repo.accept_child_invite(other, "c1", b"c1", 2026, NOW)).result == "has_parent"
    parent2 = await repo.get_account("p2")
    assert parent2 is not None
    assert await repo.children(parent2.id) == []
    assert len(await repo.open_child_invites(parent2.id, NOW)) == 1  # отказ не гасит ссылку
    assert await repo.open_child_invites(parent2.id, NOW + WEEK) == []


async def test_code_attempts_limited_per_window(repo: ProfileRepository) -> None:
    window = timedelta(hours=1)
    results = [await repo.code_attempt("u1", NOW, limit=5, window=window) for _ in range(6)]
    assert results == [True] * 5 + [False]
    assert await repo.code_attempt("u2", NOW, limit=5, window=window)
    assert await repo.code_attempt("u1", NOW + window, limit=5, window=window)
