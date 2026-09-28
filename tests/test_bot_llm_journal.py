"""Бот и журнал вызовов модели: вызовы по апдейту несут его пользователя, среду и trace_id."""

import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot import runner
from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.models import MaxUpdate
from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize, current_user_id
from hwcheck.llm.journal import JournaledLLM
from test_bot import PHOTO_UPDATE, FakeMax
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, FakeTwoStageClient, make_image


class FakeMaxWithPhoto(FakeMax):
    async def download(self, url: str) -> bytes:
        return make_image()


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_bot(tmp_path: Path, inner: Any, *, test_users: set[str]) -> tuple[Bot, Path]:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "prod", test_users=test_users)
    bot = Bot(
        FakeMaxWithPhoto(),  # type: ignore[arg-type]
        JournaledLLM(inner, log),
        InMemoryStateStore(),
        log,
        Settings(_env_file=None, photos_ttl_days=0),
    )
    return bot, path


async def test_llm_calls_of_update_carry_its_user_trace_and_models(tmp_path: Path) -> None:
    bot, path = make_bot(tmp_path, FakeTwoStageClient([TRANSCRIPT], [STRUCTURED]), test_users=set())

    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))

    events = read_events(path)
    uploaded = next(e for e in events if e["type"] == "homework_uploaded")
    calls = [e for e in events if e["type"] == "llm_call"]
    assert [(c["step"], c["model"]) for c in calls] == [
        ("vision", "GigaChat-2-Max"),
        ("vision_structure", "GigaChat-2-Pro"),
    ]
    assert {c["trace_id"] for c in calls} == {uploaded["trace_id"]}
    assert {c["user"] for c in calls} == {anonymize(42)}
    assert {c["env"] for c in calls} == {"prod"}
    # событие шага остаётся как было: на нём отчёты и разбор спорных проверок по фото
    recognized = next(e for e in events if e["type"] == "vision_recognized")
    assert (recognized["component"], recognized["calls"]) == ("vision_two_stage", 2)
    assert recognized["tokens"] == sum(c["tokens_in"] + c["tokens_out"] for c in calls)


async def test_llm_calls_of_tester_are_test_traffic(tmp_path: Path) -> None:
    tester = anonymize(42)
    assert tester is not None
    bot, path = make_bot(
        tmp_path, FakeTwoStageClient([TRANSCRIPT], [STRUCTURED]), test_users={tester}
    )

    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))

    calls = [e for e in read_events(path) if e["type"] == "llm_call"]
    assert len(calls) == 2
    assert {(c["env"], c["user"]) for c in calls} == {("test", tester)}


async def test_failed_vision_call_is_in_journal_with_error(tmp_path: Path) -> None:
    class DownClient(FakeTwoStageClient):
        async def analyze_image(self, image: bytes, **_kw: Any) -> Any:
            raise TimeoutError("api down")

    bot, path = make_bot(tmp_path, DownClient([], []), test_users=set())

    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))

    events = read_events(path)
    [call] = [e for e in events if e["type"] == "llm_call"]
    assert (call["status"], call["error"], call["step"]) == ("error", "TimeoutError", "vision")
    assert call["user"] == anonymize(42)
    assert any(e["type"] == "photo_failed" for e in events)  # прежняя обработка сбоя на месте


async def test_user_of_update_does_not_leak_into_next_update(tmp_path: Path) -> None:
    bot, _path = make_bot(
        tmp_path, FakeTwoStageClient([TRANSCRIPT], [STRUCTURED]), test_users=set()
    )
    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))
    assert current_user_id() is None


async def test_runner_gives_bot_and_subjects_the_journaled_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Раннер обязан обернуть клиента GigaChat: иначе бот работает, а журнал вызовов пуст."""
    captured: dict[str, Any] = {}

    class FakeResource:
        def __init__(self, *_args: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeResource":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def me(self) -> dict[str, Any]:
            return {"name": "bot"}

    class FakeBot:
        def __init__(self, _max: Any, llm: Any, _store: Any, events: Any, *_a: Any, **kw: Any):
            captured.update(llm=llm, events=events, subjects=kw["subjects"])

    async def no_polling(*_args: Any) -> None:
        return None

    monkeypatch.setattr(runner, "MaxClient", FakeResource)
    monkeypatch.setattr(runner, "GigaChatClient", FakeResource)
    monkeypatch.setattr(runner, "Bot", FakeBot)
    monkeypatch.setattr(runner, "_poll_loop", no_polling)
    monkeypatch.setattr(runner, "_install_stop_handler", lambda *_args: None)
    settings = Settings(
        _env_file=None,
        max_token="token",
        events_path=str(tmp_path / "events.jsonl"),
        photos_ttl_days=0,
        kb_photos_ttl_days=0,
    )

    await runner.run_polling(settings)

    assert isinstance(captured["llm"], JournaledLLM)
    assert captured["subjects"].llm is captured["llm"]  # модули предметов — через тот же журнал
    assert captured["llm"]._events is captured["events"]  # один журнал с событиями бота
