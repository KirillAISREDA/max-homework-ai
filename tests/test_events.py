"""Журнал событий: trace_id и отделение тестового трафика (антифрод, Положение п. 5.4.1)."""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from hwcheck.config import Settings
from hwcheck.events import (
    EventLog,
    anonymize,
    current_user_id,
    keyed_digest,
    legacy_anonymize,
    set_id_hash_key,
    trace,
)


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_tester_events_marked_test_in_prod(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    tester = anonymize(42)
    assert tester is not None
    log = EventLog(path, "prod", test_users={tester})
    log.log("message_received", user_id=42)
    log.log("message_received", user_id=43)
    log.log("bot_started")  # без пользователя — среда как есть
    assert [e["env"] for e in read_events(path)] == ["test", "prod", "prod"]


def test_events_share_trace_id_within_trace(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "dev")
    with trace() as first:
        log.log("homework_uploaded", user_id=42)
        log.log("vision_recognized", user_id=42)
    with trace() as second:
        log.log("message_received", user_id=42)
    log.log("outside")
    events = read_events(path)
    assert first != second
    assert [e["trace_id"] for e in events] == [first, first, second, None]


def test_settings_parse_test_users() -> None:
    settings = Settings(_env_file=None, test_users=" abc123 ,def456,, ")
    assert settings.test_user_hashes == frozenset({"abc123", "def456"})
    assert Settings(_env_file=None).test_user_hashes == frozenset()


def test_anonymize_without_key_keeps_legacy_hash() -> None:
    legacy = hashlib.sha256(b"hwcheck:42").hexdigest()[:16]
    assert anonymize(42) == legacy_anonymize(42) == legacy


def test_anonymize_with_key_is_hmac() -> None:
    """Спецификация онбординга §8: sha256 от id обратим перебором диапазона id MAX."""
    set_id_hash_key("secret-key")
    expected = hmac.new(b"secret-key", b"42", hashlib.sha256).hexdigest()[:16]
    assert anonymize(42) == expected
    assert anonymize(42) != legacy_anonymize(42)
    assert anonymize(None) is None


def test_invite_digest_and_user_hash_are_separate_domains() -> None:
    """Код из одних цифр не должен давать хэш, совпадающий с обезличенным id (F6)."""
    set_id_hash_key("secret-key")
    user = anonymize(12345678)
    assert user is not None
    assert keyed_digest("12345678") != user
    assert not keyed_digest("12345678").startswith(user)


def test_tester_listed_by_legacy_hash_is_still_test(tmp_path: Path) -> None:
    set_id_hash_key("secret-key")
    path = tmp_path / "events.jsonl"
    legacy = legacy_anonymize(42)
    assert legacy is not None
    log = EventLog(path, "prod", test_users={legacy})
    log.log("message_received", user_id=42)
    log.log("message_received", user_id=43)
    events = read_events(path)
    assert [e["env"] for e in events] == ["test", "prod"]
    assert events[0]["user"] == anonymize(42)  # в журнал пишется уже новый хэш


def test_trace_binds_user_for_components_that_do_not_know_him() -> None:
    """Обёртка над клиентом модели пользователя не знает — берёт его из контекста апдейта."""
    assert current_user_id() is None
    with trace(user_id=42):
        assert current_user_id() == 42
    assert current_user_id() is None


def test_trace_without_user_keeps_context_empty() -> None:
    with trace():
        assert current_user_id() is None


def test_explicit_user_of_event_is_not_replaced_by_context(tmp_path: Path) -> None:
    """Прежние события не меняются: пользователь события — тот, что передан явно (или никто)."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "prod")
    with trace(user_id=42):
        log.log("notify_sent", user_id=43)
        log.log("bot_started")
    assert [e["user"] for e in read_events(path)] == [anonymize(43), None]


async def test_events_of_parallel_tasks_are_whole_lines_with_own_trace(tmp_path: Path) -> None:
    """Параллельные обработки пишут в один файл из одного цикла событий: строка пишется целиком
    за один вызов без ожиданий внутри, а trace_id и пользователь у каждой задачи свои."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "prod")
    long_field = "я" * 20_000  # строка длиннее буфера записи: по частям она бы перемешалась

    async def one_update(user_id: int) -> str:
        with trace(user_id=user_id) as trace_id:
            for step in range(5):
                await asyncio.sleep(0)  # другие задачи вклиниваются между событиями
                log.log("step", user_id=current_user_id(), n=step, payload=long_field)
        return trace_id

    trace_ids = await asyncio.gather(*(one_update(user_id) for user_id in range(1, 21)))

    events = read_events(path)  # json.loads упал бы на перемешанной строке
    assert len(events) == 100
    assert len(set(trace_ids)) == 20
    for user_id, trace_id in zip(range(1, 21), trace_ids, strict=True):
        own = [e for e in events if e["trace_id"] == trace_id]
        assert [e["n"] for e in own] == [0, 1, 2, 3, 4]
        assert {e["user"] for e in own} == {anonymize(user_id)}
