"""Консоль базы знаний (спецификация каркаса §5): очередь непроверенных ответов и словари.

Ответы `y` (подтвердить), `n` (отклонить), `e` (ввести верный ответ: старый отклоняется, новый —
verified от manual), `q` (выйти). Веб-интерфейс — вне объёма.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hwcheck.db.kb import PgKnowledgeBase
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.events import EventLog
from hwcheck.subjects.kb_models import KbAnswer

KnowledgeBaseImpl = PgKnowledgeBase | InMemoryKnowledgeBase


async def review(
    kb: KnowledgeBaseImpl,
    subject: str,
    *,
    limit: int,
    read: Callable[[str], str] = input,
    write: Callable[[str], object] = print,
    events: EventLog | None = None,
) -> int:
    done = 0
    for task, answer in await kb.unverified_answers(subject, limit):
        assert answer.id is not None
        write(f"[{task.number or '—'}] {task.condition}\n  наш ответ: {answer.answer}")
        while True:
            choice = read("y — верно, n — неверно, e — исправить, q — выйти: ").strip().lower()
            if choice in ("y", "n", "e", "q"):
                break
        if choice == "q":
            break
        if choice == "y":
            await kb.set_answer_status(answer.id, "verified", checked_by="manual")
        else:
            await kb.set_answer_status(answer.id, "rejected", checked_by="manual")
            if choice == "e":
                text = read("верный ответ: ").strip()
                await kb.save_answer(
                    KbAnswer(
                        task_id=answer.task_id,
                        answer={"text": text},
                        derived_by="manual",
                        checked_by="manual",
                        status="verified",
                    )  # fmt: skip
                )
        if events is not None:
            events.log("kb_review", action=choice, subject=subject, answer_id=answer.id)
        done += 1
    return done


async def load_words(kb: KnowledgeBaseImpl, subject: str, source: str, path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    words: dict[str, dict[str, Any] | None]
    if path.suffix == ".json":
        words = json.loads(raw)
    else:
        words = {line.strip(): None for line in raw.splitlines() if line.strip()}
    await kb.add_words(subject, source, words)
    return len(words)
