"""Контракт репозитория находок (спецификация каркаса §8) — одни тесты для памяти и PostgreSQL."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from hwcheck.db.findings import FindingRecord, InMemoryFindingsRepository, PgFindingsRepository
from hwcheck.db.pool import create_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
FindingsImpl = PgFindingsRepository | InMemoryFindingsRepository


@pytest.fixture(params=["memory", "postgres"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[FindingsImpl]:
    if request.param == "memory":
        yield InMemoryFindingsRepository()
        return
    if TEST_DATABASE_URL is None:
        if "CI" in os.environ:
            pytest.fail("в CI нужен TEST_DATABASE_URL")
        pytest.skip("нужен TEST_DATABASE_URL (PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await create_pool(TEST_DATABASE_URL, server_settings={"search_path": schema})
        try:
            yield PgFindingsRepository(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


async def test_save_and_count(repo: FindingsImpl) -> None:
    now = datetime.now(UTC)
    await repo.save(
        [
            FindingRecord(
                user_hash="u1",
                subject="math",
                trace_id="t1",
                task_number="17",
                kind="arithmetic",
                strength="verified",
            ),
            FindingRecord(
                user_hash="u1",
                subject="math",
                trace_id="t1",
                task_number="18",
                kind="uncertain",
                strength="candidate",
            ),
            FindingRecord(
                user_hash="u2",
                subject="math",
                trace_id="t2",
                task_number="1",
                kind="arithmetic",
                strength="verified",
            ),
        ]  # fmt: skip
    )
    assert await repo.count_by_strength("u1", since=now - timedelta(days=1)) == {
        "verified": 1,
        "candidate": 1,
    }
    assert await repo.count_by_strength("u1", since=now + timedelta(days=1)) == {}
    await repo.save([])  # пустой список — без обращения к базе и без ошибки
