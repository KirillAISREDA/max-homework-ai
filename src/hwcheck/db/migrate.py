"""Миграции PostgreSQL при старте бота (спецификация онбординга §7).

Файлы `migrations/NNN_*.sql` применяются по порядку имён, каждый — в своей транзакции, учёт — в
`schema_migrations`. `pg_advisory_lock`: два процесса бота не применят одну миграцию дважды.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
from asyncpg.pool import PoolConnectionProxy

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x686F6D65  # «home»


async def apply_migrations(
    conn: asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record],
    directory: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Применяет новые миграции и возвращает их имена; упавшая откатывается целиком."""
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
