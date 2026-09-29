"""Итоги проверок за период — данные отчёта родителю (bot/report.py).

Источник — только таблица `homeworks`: счётчики заданий и разборов. Текста заданий, ответов и
фото в ней нет, поэтому их нет и в отчёте (152-ФЗ). Запрос живёт отдельно от db/repo.py: там —
всё, что нужно онбордингу, и файл уже большой.

`PgReportRepository` — PostgreSQL; в памяти тот же контракт выполняет
`InMemoryProfileRepository` (db/memory.py). Обе реализации проходят одни тесты
(tests/test_report_repo.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg

from hwcheck.db.repo import HomeworkCounts


@dataclass(frozen=True)
class SubjectTotals:
    """Проверки одного ребёнка по одному предмету за период."""

    student_id: int  # профиль ребёнка, не аккаунт MAX
    subject: str
    homeworks: int
    counts: HomeworkCounts
    errors_resolved: int  # разборов до верного ответа; бывает больше, чем ошибок


class ReportRepository(Protocol):
    async def totals(
        self, parent_user_id: int, since: datetime, until: datetime
    ) -> list[SubjectTotals]:
        """Итоги по детям родителя за период: начало входит, конец — нет. По порядку детей и
        предметов; ребёнок без проверок в ответ не попадает. Согласие здесь не проверяется —
        кого показывать, решает отчёт."""
        ...


# владение ребёнком — в самом запросе: id профиля снаружи не приходит, только id родителя
_TOTALS = (
    "SELECT h.student_id, h.subject, count(*)::int AS homeworks, "
    "sum(h.tasks_total)::int AS total, sum(h.tasks_correct)::int AS correct, "
    "sum(h.tasks_wrong)::int AS wrong, sum(h.tasks_uncertain)::int AS uncertain, "
    "sum(h.errors_resolved)::int AS resolved "
    "FROM homeworks h JOIN student_profiles p ON p.id = h.student_id "
    "WHERE p.parent_user_id = $1 AND h.created_at >= $2 AND h.created_at < $3 "
    "GROUP BY h.student_id, h.subject ORDER BY h.student_id, h.subject"
)


def _totals(row: asyncpg.Record) -> SubjectTotals:
    counts = HomeworkCounts(
        total=row["total"],
        correct=row["correct"],
        wrong=row["wrong"],
        uncertain=row["uncertain"],
    )
    return SubjectTotals(
        student_id=row["student_id"],
        subject=row["subject"],
        homeworks=row["homeworks"],
        counts=counts,
        errors_resolved=row["resolved"],
    )


class PgReportRepository:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def totals(
        self, parent_user_id: int, since: datetime, until: datetime
    ) -> list[SubjectTotals]:
        rows = await self._pool.fetch(_TOTALS, parent_user_id, since, until)
        return [_totals(row) for row in rows]
