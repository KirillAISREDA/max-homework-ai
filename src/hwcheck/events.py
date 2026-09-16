"""Журнал событий: конкурсный учёт обращений (Положение, Прил. 2 п. 2.2 и п. 5).

Каждый вызов компонента (LLM, инструмент, шаг пайплайна) — событие с обезличенным
id пользователя, типом, результатом и trace_id (все вызовы одного апдейта MAX).
environment=dev исключается из зачёта; в prod события тестеров (`test_users`) пишутся
с env=test — тестовый трафик отделён на уровне каждой записи (антифрод, п. 5.4).
Прод-хранилище — PostgreSQL events (арх. §6.1); на пилоте — JSONL, формат совместимый.
"""

import hashlib
import hmac
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
        # TEST_USERS мог быть собран до перехода на HMAC — узнаём тестера и по старому хэшу
        is_tester = user in self._test_users or legacy_anonymize(user_id) in self._test_users
        record = {
            "ts": time.time(),
            "env": "test" if is_tester else self._environment,
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

    События до появления поля reason считаются причиной «unknown». Пересчёт после ответа
    ученика (task_clarified) — отдельно в `clarified`: задание уже учтено в `verdicts`.
    """
    summary: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        if row.get("type") not in ("task_checked", "task_clarified"):
            continue
        env = summary.setdefault(
            str(row.get("env")), {"verdicts": {}, "uncertain_reasons": {}, "clarified": {}}
        )
        verdict = str(row.get("verdict"))
        if row.get("type") == "task_clarified":
            env["clarified"][verdict] = env["clarified"].get(verdict, 0) + 1
            continue
        env["verdicts"][verdict] = env["verdicts"].get(verdict, 0) + 1
        if verdict == "uncertain":
            reason = str(row.get("reason") or "unknown")
            env["uncertain_reasons"][reason] = env["uncertain_reasons"].get(reason, 0) + 1
    return summary


_id_hash_key: bytes | None = None
_INVITE_DOMAIN = b"invite:"


def set_id_hash_key(key: str | None) -> None:
    """Секрет HMAC для обезличивания id (`ID_HASH_KEY`); None — legacy-хэш (локально, тесты)."""
    global _id_hash_key
    _id_hash_key = key.encode() if key else None


def anonymize(user_id: int | None) -> str | None:
    """152-ФЗ и антифрод: наружу — только необратимый хэш идентификатора.

    С ключом — HMAC (спецификация онбординга §8): простой sha256 от id обратим перебором
    диапазона id MAX. Тот же хэш — поле user в events.jsonl, ключи Redis и имена фото.
    """
    if user_id is None:
        return None
    if _id_hash_key is None:
        return legacy_anonymize(user_id)
    return hmac.new(_id_hash_key, str(user_id).encode(), hashlib.sha256).hexdigest()[:16]


def keyed_digest(value: str) -> str:
    """Полный HMAC-SHA256 секрета с малым перебором (запасной код приглашения: 32⁸ ≈ 2⁴⁰), чтобы
    хэш из утёкшей базы или бэкапа не подбирался без ключа; без ключа (локально) — sha256.

    Домен `invite:` отделяет эти хэши от `anonymize` на том же ключе: иначе у кода из одних цифр
    начало хэша совпало бы с обезличенным id."""
    message = _INVITE_DOMAIN + value.encode()
    if _id_hash_key is None:
        return hashlib.sha256(message).hexdigest()
    return hmac.new(_id_hash_key, message, hashlib.sha256).hexdigest()


def legacy_anonymize(user_id: int | None) -> str | None:
    """Хэш до перехода на HMAC — только чтобы узнать тестеров из старого TEST_USERS."""
    if user_id is None:
        return None
    return hashlib.sha256(f"hwcheck:{user_id}".encode()).hexdigest()[:16]
