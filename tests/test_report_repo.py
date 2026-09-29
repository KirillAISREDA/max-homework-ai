"""Контракт хранилища отчёта родителю: итоги проверок по детям и предметам за период.

Одни тесты для InMemoryProfileRepository и PgReportRepository — та же фикстура, что у
tests/test_profile_repo.py: без TEST_DATABASE_URL вариант PostgreSQL пропускается, в CI он
обязателен.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.repo import HomeworkCounts, PgProfileRepository, ProfileRepository
from hwcheck.db.reports import PgReportRepository, ReportRepository, SubjectTotals
from test_profile_repo import repo  # noqa: F401 — фикстура обеих реализаций

SINCE = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)
UNTIL = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
INSIDE = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
MOMENT = timedelta(microseconds=1)


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
