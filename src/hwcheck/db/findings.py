"""Находки проверки в PostgreSQL (спецификация каркаса §8): аналитика качества по предметам и
основа модели ученика. Персональных данных нет — только хэш пользователя и коды."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import asyncpg
from pydantic import BaseModel

from hwcheck.subjects.base import Strength


class FindingRecord(BaseModel):
    user_hash: str
    subject: str
    trace_id: str | None
    task_number: str | None
    kind: str
    strength: Strength
    rule_code: str | None = None
    confirmed: bool | None = None
    resolved: bool = False
    created_at: datetime | None = None


class FindingsRepository(Protocol):
    async def save(self, records: list[FindingRecord]) -> None: ...

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]: ...


class InMemoryFindingsRepository:
    def __init__(self) -> None:
        self.saved: list[FindingRecord] = []

    async def save(self, records: list[FindingRecord]) -> None:
        now = datetime.now(UTC)
        self.saved.extend(r.model_copy(update={"created_at": now}) for r in records)

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.saved:
            if r.user_hash == user_hash and r.created_at is not None and r.created_at >= since:
                counts[r.strength] = counts.get(r.strength, 0) + 1
        return counts


class PgFindingsRepository:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def save(self, records: list[FindingRecord]) -> None:
        if not records:
            return
        await self._pool.executemany(
            "INSERT INTO findings (user_hash, subject, trace_id, task_number, kind, strength, "
            "rule_code, confirmed, resolved) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            [
                (
                    r.user_hash,
                    r.subject,
                    r.trace_id,
                    r.task_number,
                    r.kind,
                    r.strength,
                    r.rule_code,
                    r.confirmed,
                    r.resolved,
                )  # fmt: skip
                for r in records
            ],
        )

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT strength, count(*) AS n FROM findings WHERE user_hash = $1 "
            "AND created_at >= $2 GROUP BY strength",
            user_hash,
            since,
        )
        return {r["strength"]: r["n"] for r in rows}
