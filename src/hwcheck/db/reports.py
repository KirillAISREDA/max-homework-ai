"""Итоги проверок за период — данные отчёта родителю (bot/report.py), а для отчёта раз в
неделю — выключатель, получатели и отметка недели (bot/report_schedule.py).

Источник итогов — только таблица `homeworks`: счётчики заданий и разборов. Текста заданий,
ответов и фото в ней нет, поэтому их нет и в отчёте (152-ФЗ). Запросы живут отдельно от
db/repo.py: там — всё, что нужно онбордингу, и файл уже большой.

Отметка недели (`weekly_reports`) ставится до отправки, как marker опроса MAX сохраняется до
обработки: доставка «не больше одного раза». Процесс упал между отметкой и отправкой — этот
отчёт потерян (отметка так и останется `sending`), но второй раз не уйдёт.

`PgReportRepository` — PostgreSQL; в памяти тот же контракт выполняет
`InMemoryProfileRepository` (db/memory.py). Обе реализации проходят одни тесты
(tests/test_report_repo.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import asyncpg

# строку аккаунта читает тот же разборщик, что у хранилища профилей: получатель отчёта — тот
# же `Account`, что видит онбординг
from hwcheck.db.repo import Account, HomeworkCounts, _account

# чем кончилась неделя у родителя; до этого отметка — `sending`
WeeklyStatus = Literal["sent", "failed", "skipped"]


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

    async def weekly_enabled(self, parent_user_id: int) -> bool:
        """Присылать ли отчёт раз в неделю; настройку ещё не трогал — присылать."""
        ...

    async def set_weekly(self, parent_user_id: int, enabled: bool) -> None:
        """Выключатель отчёта раз в неделю. Режим итогов (`notify_mode`) не трогает."""
        ...

    async def due_parents(self, slot: datetime) -> list[Account]:
        """Кому отправить отчёт недели, кончившейся в `slot`: родители с включённым отчётом, у
        которых есть ребёнок с действующим согласием, данным до `slot`, и нет отметки этой
        недели. По порядку аккаунтов."""
        ...

    async def claim_weekly(self, parent_user_id: int, slot: datetime) -> bool:
        """Отметка недели перед отправкой; False — отметка уже есть, отчёт не отправлять."""
        ...

    async def finish_weekly(
        self, parent_user_id: int, slot: datetime, status: WeeklyStatus
    ) -> None:
        """Чем кончилась отправка. Отметки нет — ничего не происходит."""
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
# согласие по любой версии политики: отчёт собирается из счётчиков и никуда не передаётся
_DUE_PARENTS = (
    "SELECT u.* FROM users u LEFT JOIN parent_settings s ON s.user_id = u.id "
    "WHERE u.role = 'parent' AND COALESCE(s.weekly_report, true) "
    "AND EXISTS (SELECT 1 FROM student_profiles p JOIN consents c "
    "ON c.student_profile_id = p.id AND c.revoked_at IS NULL "
    "WHERE p.parent_user_id = u.id AND c.given_at < $1) "
    "AND NOT EXISTS (SELECT 1 FROM weekly_reports r "
    "WHERE r.parent_user_id = u.id AND r.period_end = $1) "
    "ORDER BY u.id"
)
_CLAIM = (
    "INSERT INTO weekly_reports (parent_user_id, period_end) VALUES ($1, $2) "
    "ON CONFLICT DO NOTHING RETURNING parent_user_id"
)
_FINISH = (
    "UPDATE weekly_reports SET status = $3, finished_at = now() "
    "WHERE parent_user_id = $1 AND period_end = $2"
)
# строки настроек может не быть; режим итогов в существующей строке остаётся как был
_SET_WEEKLY = (
    "INSERT INTO parent_settings (user_id, weekly_report) VALUES ($1, $2) "
    "ON CONFLICT (user_id) DO UPDATE SET weekly_report = EXCLUDED.weekly_report"
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

    async def weekly_enabled(self, parent_user_id: int) -> bool:
        enabled: bool | None = await self._pool.fetchval(
            "SELECT weekly_report FROM parent_settings WHERE user_id = $1", parent_user_id
        )
        return enabled if enabled is not None else True

    async def set_weekly(self, parent_user_id: int, enabled: bool) -> None:
        await self._pool.execute(_SET_WEEKLY, parent_user_id, enabled)

    async def due_parents(self, slot: datetime) -> list[Account]:
        rows = await self._pool.fetch(_DUE_PARENTS, slot)
        return [_account(row) for row in rows]

    async def claim_weekly(self, parent_user_id: int, slot: datetime) -> bool:
        claimed = await self._pool.fetchval(_CLAIM, parent_user_id, slot)
        return claimed is not None

    async def finish_weekly(
        self, parent_user_id: int, slot: datetime, status: WeeklyStatus
    ) -> None:
        await self._pool.execute(_FINISH, parent_user_id, slot, status)
