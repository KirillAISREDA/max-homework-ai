"""Контракт хранилища отчёта родителю: итоги проверок по детям и предметам за период, а для
отчёта раз в неделю — выключатель, получатели и отметка недели.

Одни тесты для InMemoryProfileRepository и PgReportRepository — та же фикстура, что у
tests/test_profile_repo.py: без TEST_DATABASE_URL вариант PostgreSQL пропускается, в CI он
обязателен.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hwcheck.bot.invites import new_invite
from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.repo import HomeworkCounts, PgProfileRepository, ProfileRepository
from hwcheck.db.reports import PgReportRepository, ReportRepository, SubjectTotals
from test_profile_repo import repo  # noqa: F401 — фикстура обеих реализаций

SINCE = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)
UNTIL = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
INSIDE = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
MOMENT = timedelta(microseconds=1)
SLOT = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)  # воскресенье 27.09.2026, 18:00 мск
NEXT_SLOT = SLOT + timedelta(days=7)


def reports_of(repo: ProfileRepository) -> ReportRepository:  # noqa: F811
    """Отчёт читает ту же базу, в которую пишет хранилище профилей."""
    if isinstance(repo, PgProfileRepository):
        return PgReportRepository(repo._pool)
    assert isinstance(repo, InMemoryProfileRepository)
    return repo


async def homework_at(
    repo: ProfileRepository,  # noqa: F811
    student_id: int,
    when: datetime,
    counts: HomeworkCounts,
    *,
    subject: str = "math",
    resolved: int = 0,
) -> None:
    """Проверка с заданным временем: в базе время ставит `now()`, в памяти — часы хранилища."""
    homework = await repo.add_homework(student_id, subject, counts)
    assert homework is not None
    for _ in range(resolved):
        await repo.resolve_error(homework.id)
    if isinstance(repo, PgProfileRepository):
        await repo._pool.execute(
            "UPDATE homeworks SET created_at = $2 WHERE id = $1", homework.id, when
        )
    else:
        assert isinstance(repo, InMemoryProfileRepository)
        repo.homeworks[homework.id] = replace(repo.homeworks[homework.id], created_at=when)


async def family(repo: ProfileRepository, parent_hash: str = "p1") -> tuple[int, int, int]:  # noqa: F811
    """Родитель и двое его детей 1–4 класса: (id родителя, id старшего, id младшего)."""
    parent = await repo.get_or_create_account(parent_hash, b"p", "parent")
    first = await repo.start_child_by_parent(parent.id, 3, 2026)
    assert await repo.give_parent_consent(first.id, parent_hash, "v2", INSIDE)
    second = await repo.start_child_by_parent(parent.id, 4, 2026)
    assert await repo.give_parent_consent(second.id, parent_hash, "v2", INSIDE)
    return parent.id, first.id, second.id


async def test_totals_are_grouped_by_child_and_subject(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, second = await family(repo)
    await homework_at(repo, first, INSIDE, HomeworkCounts(5, 3, 2, 0), resolved=1)
    await homework_at(repo, first, INSIDE + timedelta(days=1), HomeworkCounts(4, 2, 1, 1))
    await homework_at(repo, first, INSIDE, HomeworkCounts(3, 3, 0, 0), subject="russian")
    await homework_at(repo, second, INSIDE, HomeworkCounts(2, 1, 1, 0), resolved=2)

    assert await reports_of(repo).totals(parent, SINCE, UNTIL) == [
        SubjectTotals(first, "math", 2, HomeworkCounts(9, 5, 3, 1), errors_resolved=1),
        SubjectTotals(first, "russian", 1, HomeworkCounts(3, 3, 0, 0), errors_resolved=0),
        SubjectTotals(second, "math", 1, HomeworkCounts(2, 1, 1, 0), errors_resolved=2),
    ]


async def test_period_includes_its_start_and_excludes_its_end(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, _ = await family(repo)
    await homework_at(repo, first, SINCE - MOMENT, HomeworkCounts(1, 1, 0, 0))  # до периода
    await homework_at(repo, first, SINCE, HomeworkCounts(2, 2, 0, 0))
    await homework_at(repo, first, UNTIL - MOMENT, HomeworkCounts(4, 4, 0, 0))
    await homework_at(repo, first, UNTIL, HomeworkCounts(8, 8, 0, 0))  # уже следующий период

    assert await reports_of(repo).totals(parent, SINCE, UNTIL) == [
        SubjectTotals(first, "math", 2, HomeworkCounts(6, 6, 0, 0), errors_resolved=0)
    ]


async def test_children_of_another_parent_are_not_included(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, _ = await family(repo)
    other, foreign, _ = await family(repo, "p2")
    await homework_at(repo, first, INSIDE, HomeworkCounts(2, 2, 0, 0))
    await homework_at(repo, foreign, INSIDE, HomeworkCounts(7, 1, 6, 0))

    assert await reports_of(repo).totals(parent, SINCE, UNTIL) == [
        SubjectTotals(first, "math", 1, HomeworkCounts(2, 2, 0, 0), errors_resolved=0)
    ]
    assert [row.student_id for row in await reports_of(repo).totals(other, SINCE, UNTIL)] == [
        foreign
    ]


async def test_child_with_own_account_belongs_to_his_parent(repo: ProfileRepository) -> None:  # noqa: F811
    """Ребёнок 5–9 класса присылает домашку сам: в отчёт родителя она идёт по связке."""
    profile = await repo.create_student("s1", b"s1", 7, 2026)
    assert profile is not None
    await homework_at(repo, profile.id, INSIDE, HomeworkCounts(3, 2, 1, 0))
    assert profile.user_id is not None
    # аккаунт самого ребёнка — не родитель: по его id отчёта нет
    assert await reports_of(repo).totals(profile.user_id, SINCE, UNTIL) == []


async def test_no_homework_gives_empty_totals(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, _ = await family(repo)
    assert await reports_of(repo).totals(parent, SINCE, UNTIL) == []
    await homework_at(repo, first, INSIDE, HomeworkCounts(2, 2, 0, 0))
    assert await reports_of(repo).totals(parent, UNTIL, UNTIL + timedelta(days=7)) == []
    assert await reports_of(repo).totals(parent + 100, SINCE, UNTIL) == []


async def test_homework_remembers_when_it_was_checked(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, _ = await family(repo)
    before = datetime.now(UTC) - timedelta(minutes=5)
    homework = await repo.add_homework(first, "math", HomeworkCounts(1, 1, 0, 0))
    assert homework is not None and homework.created_at is not None
    assert before < homework.created_at < datetime.now(UTC) + timedelta(minutes=5)
    resolved = await repo.resolve_error(homework.id)
    assert resolved is not None and resolved.created_at == homework.created_at
    week = timedelta(days=7)
    [row] = await reports_of(repo).totals(parent, before, before + week)
    assert (row.student_id, row.homeworks) == (first, 1)


async def test_memory_repository_takes_time_from_its_clock() -> None:
    """Сценарные тесты двигают часы набора: домашка в памяти получает их время."""
    now = [INSIDE]
    memory = InMemoryProfileRepository(clock=lambda: now[0])
    parent, first, _ = await family(memory)
    old = await memory.add_homework(first, "math", HomeworkCounts(1, 1, 0, 0))
    now[0] = UNTIL
    late = await memory.add_homework(first, "math", HomeworkCounts(2, 2, 0, 0))
    assert old is not None and late is not None
    assert (old.created_at, late.created_at) == (INSIDE, UNTIL)
    assert await memory.totals(parent, SINCE, UNTIL) == [
        SubjectTotals(first, "math", 1, HomeworkCounts(1, 1, 0, 0), errors_resolved=0)
    ]


# --- отчёт раз в неделю: выключатель, получатели, отметка недели ---


async def weekly_status(repo: ProfileRepository, parent: int, slot: datetime) -> str | None:  # noqa: F811
    """Отметка недели у родителя; None — отметки нет. Читать её боту незачем, только тесту."""
    if isinstance(repo, PgProfileRepository):
        status: str | None = await repo._pool.fetchval(
            "SELECT status FROM weekly_reports WHERE parent_user_id = $1 AND period_end = $2",
            parent,
            slot,
        )
        return status
    assert isinstance(repo, InMemoryProfileRepository)
    return repo.weekly_reports.get((parent, slot))


async def revoke(repo: ProfileRepository, profile_id: int, when: datetime) -> None:  # noqa: F811
    if isinstance(repo, PgProfileRepository):
        await repo._pool.execute(
            "UPDATE consents SET revoked_at = $2 WHERE student_profile_id = $1", profile_id, when
        )
        return
    assert isinstance(repo, InMemoryProfileRepository)
    for consent in repo.consents:
        if consent.profile_id == profile_id:
            consent.revoked_at = when


async def due(repo: ProfileRepository, slot: datetime = SLOT) -> list[int]:  # noqa: F811
    return [account.id for account in await reports_of(repo).due_parents(slot)]


async def test_weekly_report_is_on_until_parent_switches_it_off(repo: ProfileRepository) -> None:  # noqa: F811
    parent, _, _ = await family(repo)
    other, _, _ = await family(repo, "p2")
    reports = reports_of(repo)
    assert await reports.weekly_enabled(parent)  # строки настроек ещё нет
    assert await reports.weekly_enabled(parent + 100)  # и аккаунта тоже

    await reports.set_weekly(parent, False)
    assert not await reports.weekly_enabled(parent)
    assert await reports.weekly_enabled(other)  # выключил только себе
    await reports.set_weekly(parent, False)  # второе нажатие
    assert not await reports.weekly_enabled(parent)
    await reports.set_weekly(parent, True)
    assert await reports.weekly_enabled(parent)


async def test_weekly_switch_does_not_touch_notifications(repo: ProfileRepository) -> None:  # noqa: F811
    """Два выключателя — в одной строке настроек: запись одного не сбрасывает другой."""
    parent, _, _ = await family(repo)
    reports = reports_of(repo)

    await reports.set_weekly(parent, False)  # строки настроек не было
    assert await repo.notify_mode(parent) == "instant"
    await repo.set_notify_mode(parent, "off")
    await reports.set_weekly(parent, True)
    assert await repo.notify_mode(parent) == "off"
    await reports.set_weekly(parent, False)
    assert await repo.notify_mode(parent) == "off"


async def test_notification_switch_does_not_touch_weekly_report(repo: ProfileRepository) -> None:  # noqa: F811
    parent, _, _ = await family(repo)
    other, _, _ = await family(repo, "p2")
    reports = reports_of(repo)

    await repo.set_notify_mode(other, "off")  # строки настроек не было
    assert await reports.weekly_enabled(other)
    await reports.set_weekly(parent, False)
    await repo.set_notify_mode(parent, "off")
    assert not await reports.weekly_enabled(parent)
    await repo.set_notify_mode(parent, "instant")
    assert not await reports.weekly_enabled(parent)


async def test_due_parents_are_parents_with_consent_in_order(repo: ProfileRepository) -> None:  # noqa: F811
    first, _, _ = await family(repo)
    second, _, _ = await family(repo, "p2")
    accounts = await reports_of(repo).due_parents(SLOT)
    assert accounts == [await repo.account_by_id(first), await repo.account_by_id(second)]
    assert [account.role for account in accounts] == ["parent", "parent"]
    assert [account.user_hash for account in accounts] == ["p1", "p2"]
    assert all(account.user_id_enc == b"p" for account in accounts)


async def test_parent_of_child_with_own_account_is_due(repo: ProfileRepository) -> None:  # noqa: F811
    """Согласие по любой версии политики; ребёнок 5–9 класса присылает домашку сам."""
    profile = await repo.create_student("s1", b"s1", 7, 2026)
    assert profile is not None and profile.user_id is not None
    invite = new_invite("student_invites_parent")
    await repo.create_invite(invite, profile.user_id, INSIDE + timedelta(days=7))
    outcome = await repo.accept_parent_invite(invite.token_hash, "p1", b"p", "v0", INSIDE)
    assert outcome.result == "ok"
    parent = await repo.get_account("p1")
    assert parent is not None
    assert await due(repo) == [parent.id]  # ученик — не получатель


async def test_switched_off_parent_is_not_due(repo: ProfileRepository) -> None:  # noqa: F811
    parent, _, _ = await family(repo)
    other, _, _ = await family(repo, "p2")
    await reports_of(repo).set_weekly(parent, False)
    await repo.set_notify_mode(other, "off")  # итоги выключены, отчёт — нет
    assert await due(repo) == [other]
    await reports_of(repo).set_weekly(parent, True)
    assert await due(repo) == [parent, other]


async def test_parent_without_children_or_consent_is_not_due(repo: ProfileRepository) -> None:  # noqa: F811
    await repo.get_or_create_account("childless", b"p", "parent")
    waiting = await repo.get_or_create_account("waiting", b"p", "parent")
    await repo.start_child_by_parent(waiting.id, 2, 2026)  # согласие ещё не дано
    await repo.create_student("s1", b"s1", 7, 2026)  # ученик без родителя
    assert await due(repo) == []


async def test_revoked_consent_takes_parent_out(repo: ProfileRepository) -> None:  # noqa: F811
    parent, first, second = await family(repo)
    await revoke(repo, first, INSIDE + timedelta(hours=1))
    assert await due(repo) == [parent]  # у второго ребёнка согласие действует
    await revoke(repo, second, INSIDE + timedelta(hours=1))
    assert await due(repo) == []


async def test_consent_after_the_slot_waits_for_the_next_week(repo: ProfileRepository) -> None:  # noqa: F811
    early = await repo.get_or_create_account("early", b"p", "parent")
    child = await repo.start_child_by_parent(early.id, 3, 2026)
    assert await repo.give_parent_consent(child.id, "early", "v2", SLOT - MOMENT)
    late = await repo.get_or_create_account("late", b"p", "parent")
    child = await repo.start_child_by_parent(late.id, 3, 2026)
    assert await repo.give_parent_consent(child.id, "late", "v2", SLOT)  # в час рассылки

    assert await due(repo) == [early.id]
    assert await due(repo, NEXT_SLOT) == [early.id, late.id]


async def test_claimed_parent_is_not_due_for_that_slot_only(repo: ProfileRepository) -> None:  # noqa: F811
    parent, _, _ = await family(repo)
    other, _, _ = await family(repo, "p2")
    reports = reports_of(repo)

    assert await reports.claim_weekly(parent, SLOT)
    assert await due(repo) == [other]
    assert await due(repo, NEXT_SLOT) == [parent, other]  # отметка другой недели не мешает
    assert await reports.claim_weekly(other, NEXT_SLOT)
    assert await due(repo) == [other]
    assert await due(repo, NEXT_SLOT) == [parent]


async def test_week_is_claimed_once(repo: ProfileRepository) -> None:  # noqa: F811
    parent, _, _ = await family(repo)
    other, _, _ = await family(repo, "p2")
    reports = reports_of(repo)

    assert await reports.claim_weekly(parent, SLOT)
    assert await weekly_status(repo, parent, SLOT) == "sending"  # отметка — до отправки
    assert not await reports.claim_weekly(parent, SLOT)
    assert await reports.claim_weekly(parent, NEXT_SLOT)
    assert await reports.claim_weekly(other, SLOT)
    await reports.finish_weekly(parent, SLOT, "sent")
    assert not await reports.claim_weekly(parent, SLOT)  # и после отправки
    assert await weekly_status(repo, parent, SLOT) == "sent"  # повторная отметка статус не сбила


async def test_finished_week_keeps_its_status(repo: ProfileRepository) -> None:  # noqa: F811
    parents = [(await family(repo, name))[0] for name in ("p1", "p2", "p3", "p4")]
    reports = reports_of(repo)
    for parent in parents:
        assert await reports.claim_weekly(parent, SLOT)
    sent, failed, skipped, crashed = parents

    await reports.finish_weekly(sent, SLOT, "sent")
    await reports.finish_weekly(failed, SLOT, "failed")
    await reports.finish_weekly(skipped, SLOT, "skipped")
    statuses = [await weekly_status(repo, parent, SLOT) for parent in parents]
    assert statuses == ["sent", "failed", "skipped", "sending"]  # упавший посреди отправки
    assert await weekly_status(repo, crashed, SLOT) == "sending"
    assert await due(repo) == []  # и он второй раз отчёт не получит

    await reports.finish_weekly(sent, NEXT_SLOT, "sent")  # отметки нет — нечего и закрывать
    assert await weekly_status(repo, sent, NEXT_SLOT) is None
    assert await due(repo, NEXT_SLOT) == parents
