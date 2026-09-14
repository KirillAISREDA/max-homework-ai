"""Миграции PostgreSQL на пустой схеме (спецификация онбординга §6, §15).

Локально без TEST_DATABASE_URL — пропуск; в CI база обязательна, иначе пропуск спрятал бы
непроверенные миграции.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from hwcheck.db.migrate import apply_migrations
from hwcheck.db.pool import create_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None and "CI" not in os.environ,
    reason="нужен TEST_DATABASE_URL (PostgreSQL)",
)
TABLES = {
    "schema_migrations", "users", "student_profiles", "parent_settings", "consents", "invites",
    "homeworks", "subject_waitlist", "login_attempts",
}  # fmt: skip


def database_url() -> str:
    assert TEST_DATABASE_URL is not None, "в CI нужен TEST_DATABASE_URL"
    return TEST_DATABASE_URL


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    """Своя схема на тест: миграции проверяются на пустой базе, тесты не мешают друг другу."""
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(database_url())
    await admin.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def connect(schema: str) -> asyncpg.Connection[asyncpg.Record]:
    return await asyncpg.connect(database_url(), server_settings={"search_path": schema})


async def test_migrations_create_schema_once(schema: str) -> None:
    conn = await connect(schema)
    try:
        assert await apply_migrations(conn) == ["001_onboarding.sql"]
        assert await apply_migrations(conn) == []
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1", schema
        )
        assert {row["table_name"] for row in rows} == TABLES
    finally:
        await conn.close()


async def test_constraints_protect_consent_and_cascade_profiles(schema: str) -> None:
    conn = await connect(schema)
    consent = (
        "INSERT INTO consents (parent_hash, student_hash, policy_version, given_at) "
        "VALUES ($1, 's1', 'v1', now())"
    )
    try:
        await apply_migrations(conn)
        student = await conn.fetchval(
            "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
            "VALUES ('s1', 'x', 'student') RETURNING id"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO student_profiles (user_id, grade) VALUES ($1, 10)", student
            )
        await conn.execute(consent, "p1")
        with pytest.raises(asyncpg.UniqueViolationError):  # у ребёнка один подтвердивший родитель
            await conn.execute(consent, "p2")
        await conn.execute("UPDATE consents SET revoked_at = now() WHERE parent_hash = 'p1'")
        await conn.execute(consent, "p2")
        await conn.execute(
            "INSERT INTO student_profiles (user_id, grade, subject) VALUES ($1, 4, 'math')", student
        )
        await conn.execute(
            "INSERT INTO homeworks (student_user_id, subject, tasks_total, tasks_correct, "
            "tasks_wrong, tasks_uncertain) VALUES ($1, 'math', 3, 2, 1, 0)",
            student,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", student)
        assert await conn.fetchval("SELECT count(*) FROM student_profiles") == 0
        assert await conn.fetchval("SELECT count(*) FROM homeworks") == 0
        assert await conn.fetchval("SELECT count(*) FROM consents") == 2  # запись согласия остаётся
    finally:
        await conn.close()


async def test_failed_migration_rolls_back_whole_file(schema: str, tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE first_table (id int);", encoding="utf-8")
    (tmp_path / "002_broken.sql").write_text(
        "CREATE TABLE half_table (id int);\nSELECT broken(;", encoding="utf-8"
    )
    conn = await connect(schema)
    try:
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await apply_migrations(conn, tmp_path)
        names = [row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")]
        assert names == ["001_ok.sql"]
        assert await conn.fetchval("SELECT to_regclass('half_table')") is None
    finally:
        await conn.close()


async def test_create_pool_applies_migrations(schema: str) -> None:
    pool = await create_pool(database_url(), server_settings={"search_path": schema})
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 1
    finally:
        await pool.close()
