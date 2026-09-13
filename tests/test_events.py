"""Журнал событий: trace_id и отделение тестового трафика (антифрод, Положение п. 5.4)."""

import json
from pathlib import Path
from typing import Any

from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize, trace


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
