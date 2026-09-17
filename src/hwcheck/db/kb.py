"""База знаний в PostgreSQL (спецификация каркаса §5): страницы учебников, задания, наши ответы,
правила, словари. Поиск страницы — по отпечатку нормализованного текста."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import asyncpg

from hwcheck.subjects.kb_models import AnswerStatus, KbAnswer, KbPage, KbRule, KbTask

_KEEP = re.compile(r"[^0-9a-zа-я ]+")


def fingerprint(text: str) -> str:
    """Один и тот же учебный текст с разных фото даёт один отпечаток: регистр, ё, пунктуация и
    переносы не важны; порядок слов — важен."""
    normalized = " ".join(_KEEP.sub(" ", text.lower().replace("ё", "е")).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _task(row: asyncpg.Record) -> KbTask:
    return KbTask(
        id=row["id"], number=row["number"], condition=row["condition"], task_kind=row["task_kind"]
    )


async def _find_page(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record], subject: str, key: str
) -> KbPage | None:
    """Страница по готовому отпечатку на переданном соединении: вызывается и из `save_page`,
    внутри уже открытой транзакции."""
    row = await conn.fetchrow(
        "SELECT * FROM kb_pages WHERE subject = $1 AND fingerprint = $2", subject, key
    )
    if row is None:
        return None
    tasks = await conn.fetch("SELECT * FROM kb_tasks WHERE page_id = $1 ORDER BY id", row["id"])
    return KbPage(
        id=row["id"],
        subject=row["subject"],
        grade=row["grade"],
        fingerprint=row["fingerprint"],
        text=row["text"],
        photo_path=row["photo_path"],
        tasks=[_task(t) for t in tasks],
    )


def _answer(row: asyncpg.Record) -> KbAnswer:
    return KbAnswer(
        id=row["id"],
        task_id=row["task_id"],
        answer=json.loads(row["answer"]),
        derived_by=row["derived_by"],
        checked_by=row["checked_by"],
        status=row["status"],
        reviewed_at=row["reviewed_at"],
    )


class PgKnowledgeBase:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def find_page(self, subject: str, text: str) -> KbPage | None:
        async with self._pool.acquire() as conn:
            return await _find_page(conn, subject, fingerprint(text))

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage:
        # отпечаток всегда считается по тексту: переданный (устаревший, от другого текста)
        # спрятал бы страницу от поиска и от ограничения UNIQUE (subject, fingerprint)
        key = fingerprint(page.text)
        async with self._pool.acquire() as conn, conn.transaction():
            page_id = await conn.fetchval(
                "INSERT INTO kb_pages (subject, grade, fingerprint, text, photo_path) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (subject, fingerprint) DO NOTHING "
                "RETURNING id",
                page.subject,
                page.grade,
                key,
                page.text,
                page.photo_path,
            )
            if page_id is None:
                # то же фото от другого ученика: читаем по тому же соединению — второе из пула
                # внутри транзакции этой же корутины на исчерпанном пуле означает тупик
                existing = await _find_page(conn, page.subject, key)
                assert existing is not None
                return existing
            saved: list[KbTask] = []
            for task in tasks:
                task_id = await conn.fetchval(
                    "INSERT INTO kb_tasks (page_id, number, condition, task_kind) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    page_id,
                    task.number,
                    task.condition,
                    task.task_kind,
                )
                saved.append(task.model_copy(update={"id": task_id}))
            return page.model_copy(update={"id": page_id, "fingerprint": key, "tasks": saved})

    async def answers_for(self, task_id: int) -> list[KbAnswer]:
        rows = await self._pool.fetch(
            "SELECT * FROM kb_answers WHERE task_id = $1 ORDER BY id", task_id
        )
        return [_answer(r) for r in rows]

    async def save_answer(self, answer: KbAnswer) -> KbAnswer:
        # детерминированные ответы (словарь/правило) не требуют ручной проверки — статус в базе
        # сразу «verified», иначе они попали бы в очередь unverified_answers наравне со спорными.
        status = "verified" if answer.trust == "verified" else answer.status
        answer_id = await self._pool.fetchval(
            "INSERT INTO kb_answers (task_id, answer, derived_by, checked_by, status) "
            "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id",
            answer.task_id,
            json.dumps(answer.answer, ensure_ascii=False),
            answer.derived_by,
            answer.checked_by,
            status,
        )
        return answer.model_copy(update={"id": answer_id, "status": status})

    async def unverified_answers(self, subject: str, limit: int) -> list[tuple[KbTask, KbAnswer]]:
        rows = await self._pool.fetch(
            "SELECT a.*, t.id AS t_id, t.number AS t_number, t.condition AS t_condition, "
            "t.task_kind AS t_task_kind FROM kb_answers a "
            "JOIN kb_tasks t ON t.id = a.task_id JOIN kb_pages p ON p.id = t.page_id "
            "WHERE a.status = 'unverified' AND p.subject = $1 ORDER BY a.id LIMIT $2",
            subject,
            limit,
        )
        return [
            (
                KbTask(
                    id=r["t_id"],
                    number=r["t_number"],
                    condition=r["t_condition"],
                    task_kind=r["t_task_kind"],
                ),  # fmt: skip
                _answer(r),
            )
            for r in rows
        ]

    async def set_answer_status(
        self, answer_id: int, status: AnswerStatus, checked_by: str | None
    ) -> None:
        await self._pool.execute(
            "UPDATE kb_answers SET status = $2, checked_by = $3, reviewed_at = $4 WHERE id = $1",
            answer_id,
            status,
            checked_by,
            datetime.now(UTC),
        )

    async def rule(self, code: str) -> KbRule | None:
        row = await self._pool.fetchrow("SELECT * FROM kb_rules WHERE code = $1", code)
        if row is None:
            return None
        return KbRule(
            code=row["code"],
            subject=row["subject"],
            grade_from=row["grade_from"],
            title=row["title"],
            statement=row["statement"],
            example=row["example"],
            finding_kinds=list(row["finding_kinds"]),
        )

    async def add_rule(self, rule: KbRule) -> None:
        await self._pool.execute(
            "INSERT INTO kb_rules (code, subject, grade_from, title, statement, example, "
            "finding_kinds) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (code) DO UPDATE SET "
            "subject = EXCLUDED.subject, grade_from = EXCLUDED.grade_from, title = EXCLUDED.title, "
            "statement = EXCLUDED.statement, example = EXCLUDED.example, "
            "finding_kinds = EXCLUDED.finding_kinds",
            rule.code,
            rule.subject,
            rule.grade_from,
            rule.title,
            rule.statement,
            rule.example,
            rule.finding_kinds,
        )

    async def words(self, subject: str, source: str) -> set[str]:
        rows = await self._pool.fetch(
            "SELECT word FROM kb_words WHERE subject = $1 AND source = $2", subject, source
        )
        return {r["word"] for r in rows}

    async def add_words(
        self, subject: str, source: str, words: dict[str, dict[str, Any] | None]
    ) -> None:
        await self._pool.executemany(
            "INSERT INTO kb_words (subject, word, source, attrs) VALUES ($1, $2, $3, $4::jsonb) "
            "ON CONFLICT (subject, word, source) DO UPDATE SET attrs = EXCLUDED.attrs",
            [
                (subject, word, source, json.dumps(attrs) if attrs is not None else None)
                for word, attrs in words.items()
            ],
        )
