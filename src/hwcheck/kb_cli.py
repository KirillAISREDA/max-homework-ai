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
from hwcheck.subjects.russian.rules import load_rules

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
        condition, shown = _printable(task.condition), _printable(str(answer.answer))
        write(f"[{_printable(task.number or '—')}] {condition}\n  наш ответ: {shown}")
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


def _printable(text: str) -> str:
    """Условие и ответ приходят из распознавания и от LLM: управляющие последовательности
    (очистка экрана, цвета) не должны доезжать до терминала проверяющего."""
    return "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")


async def load_words(kb: KnowledgeBaseImpl, subject: str, source: str, path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    words: dict[str, dict[str, Any] | None]
    if path.suffix == ".json":
        words = _words_from_json(raw)
    else:
        words = {line.strip(): None for line in raw.splitlines() if line.strip()}
    await kb.add_words(subject, source, words)
    return len(words)


def _words_from_json(raw: str) -> dict[str, dict[str, Any] | None]:
    """Файл словаря приходит извне: не та форма останавливает загрузку понятным сообщением,
    а не падает где-то внутри INSERT."""
    error = SystemExit("load-words: ожидается JSON-объект {слово: {атрибуты} | null}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error from exc
    if not isinstance(data, dict):
        raise error
    for value in data.values():
        if value is not None and not isinstance(value, dict):
            raise error
    return data


async def load_rules_into(kb: KnowledgeBaseImpl, path: Path) -> int:
    """Загрузить карточки орфограмм в БЗ (идемпотентно: PgKnowledgeBase — ON CONFLICT (code)
    DO UPDATE, InMemoryKnowledgeBase — перезапись по коду)."""
    rules = load_rules(path)
    for rule in rules:
        await kb.add_rule(rule)
    return len(rules)
