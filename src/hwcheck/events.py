"""Журнал событий: конкурсный учёт обращений (Положение, Прил. 2 п. 2.2 и п. 5).

Каждый вызов компонента (LLM, инструмент, шаг пайплайна) — событие с обезличенным
id пользователя, типом, результатом и trace_id (все вызовы одного апдейта MAX).
environment=dev исключается из зачёта; в prod события тестеров (`test_users`) пишутся
с env=test — тестовый трафик отделён на уровне каждой записи (антифрод, п. 5.4).
Прод-хранилище — PostgreSQL events (арх. §6.1); на пилоте — JSONL, формат совместимый.
"""

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


@contextmanager
def trace() -> Iterator[str]:
    """Все события внутри блока получают один trace_id (один апдейт MAX)."""
    trace_id = uuid.uuid4().hex[:16]
    token = _trace_id.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id.reset(token)


class EventLog:
    def __init__(self, path: Path, environment: str, *, test_users: Iterable[str] = ()) -> None:
        self._path = path
        self._environment = environment
        self._test_users = frozenset(test_users)

    def log(
        self,
        event_type: str,
        *,
        user_id: int | None = None,
        component: str | None = None,
        user_initiated: bool = False,
        **fields: Any,
    ) -> None:
        user = anonymize(user_id)
        record = {
            "ts": time.time(),
            "env": "test" if user in self._test_users else self._environment,
            "trace_id": _trace_id.get(),
            "type": event_type,
            "user": user,
            "component": component,
            "user_initiated": user_initiated,
            **fields,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """Записи журнала; нет файла — пусто, битая строка (обрыв записи) пропускается."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("events: broken line %d skipped", number)


def summarize_events(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """Вердикты проверок и причины «не уверен» по среде (prod / test / dev).

    События до появления поля reason считаются причиной «unknown».
    """
    summary: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        if row.get("type") != "task_checked":
            continue
        env = summary.setdefault(str(row.get("env")), {"verdicts": {}, "uncertain_reasons": {}})
        verdict = str(row.get("verdict"))
        env["verdicts"][verdict] = env["verdicts"].get(verdict, 0) + 1
        if verdict == "uncertain":
            reason = str(row.get("reason") or "unknown")
            env["uncertain_reasons"][reason] = env["uncertain_reasons"].get(reason, 0) + 1
    return summary


def anonymize(user_id: int | None) -> str | None:
    """152-ФЗ и антифрод: наружу — только необратимый хэш идентификатора."""
    if user_id is None:
        return None
    return hashlib.sha256(f"hwcheck:{user_id}".encode()).hexdigest()[:16]
