"""Журнал событий: конкурсный учёт обращений (Положение, Прил. 2 п. 2.2 и п. 5).

Каждый вызов компонента (LLM, инструмент, шаг пайплайна) — событие с обезличенным
id пользователя, типом, результатом. environment=dev исключается из зачёта
(антифрод: тестовый трафик отделён). Прод-хранилище — PostgreSQL events (арх. §6.1);
на пилоте — JSONL, формат совместимый.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path, environment: str) -> None:
        self._path = path
        self._environment = environment

    def log(
        self,
        event_type: str,
        *,
        user_id: int | None = None,
        component: str | None = None,
        user_initiated: bool = False,
        **fields: Any,
    ) -> None:
        record = {
            "ts": time.time(),
            "env": self._environment,
            "type": event_type,
            "user": anonymize(user_id),
            "component": component,
            "user_initiated": user_initiated,
            **fields,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def anonymize(user_id: int | None) -> str | None:
    """152-ФЗ и антифрод: наружу — только необратимый хэш идентификатора."""
    if user_id is None:
        return None
    return hashlib.sha256(f"hwcheck:{user_id}".encode()).hexdigest()[:16]
