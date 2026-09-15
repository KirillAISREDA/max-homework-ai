"""Пул PostgreSQL (спецификация онбординга §7): открывается в раннере, миграции — при старте."""

from __future__ import annotations

import logging

import asyncpg

from hwcheck.db.migrate import apply_migrations

logger = logging.getLogger(__name__)


async def create_pool(
    dsn: str, *, server_settings: dict[str, str] | None = None
) -> asyncpg.Pool[asyncpg.Record]:
    """База недоступна или миграция упала — исключение: бот не стартует (health контейнера),
    а не работает без профилей и согласий."""
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, command_timeout=10, server_settings=server_settings
    )
    try:
        async with pool.acquire() as conn:
            applied = await apply_migrations(conn)
    except BaseException:
        await pool.close()
        raise
    if applied:
        logger.info("migrations applied: %s", ", ".join(applied))
    return pool
