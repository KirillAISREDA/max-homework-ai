"""Раннер с диспетчером апдейтов: опрос MAX не ждёт обработки, остановка дообрабатывает принятое,
события параллельных чатов не перемешиваются (антифрод: тестовый трафик — по пользователю)."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from conftest import GatedBot, ScriptedPoller, chat_update, settle
from hwcheck.bot import runner
from hwcheck.bot.dispatch import DispatchLimits
from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.runner import _load_marker, _poll_loop, dispatch_limits
from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize
from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.llm.journal import JournaledLLM, _step
from test_bot import FakeMax
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, make_image


def start_loop(
    poller: ScriptedPoller, bot: Any, tmp_path: Path, stop: asyncio.Event, **limits: Any
) -> asyncio.Task[None]:
    marker_path = tmp_path / "marker.txt"
    return asyncio.create_task(
        _poll_loop(poller, bot, marker_path, stop, DispatchLimits(**limits))  # type: ignore[arg-type]
    )


async def test_polling_goes_on_while_a_chat_is_being_checked(tmp_path: Path) -> None:
    """Домашка чата A проверяется минуту — сообщение чата B за это время принято и обработано."""
    bot = GatedBot()
    bot.hold("a1")
    poller = ScriptedPoller([chat_update(1, "a1")], [chat_update(2, "b1")])
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop)

    await bot.wait_finished("b1")
    await asyncio.wait_for(poller.idle.wait(), 5)
    assert bot.finished == ["b1"]
    assert _load_marker(tmp_path / "marker.txt") == 2  # marker — сразу после получения батча
    stop.set()
    await settle()
    assert not loop.done()  # принятый апдейт чата A дообрабатывается
    bot.release("a1")
    await asyncio.wait_for(loop, 5)
    assert bot.finished == ["b1", "a1"]


async def test_stop_finishes_queued_updates_too(tmp_path: Path) -> None:
    bot = GatedBot()
    bot.hold("a1")
    batch = [chat_update(1, "a1"), chat_update(1, "a2"), chat_update(2, "b1")]
    poller = ScriptedPoller(batch)
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop, concurrency=1)
    await bot.wait_started("a1")
    await asyncio.wait_for(poller.idle.wait(), 5)
    stop.set()
    await settle()
    assert not loop.done() and bot.started == ["a1"]
    bot.release("a1")

    await asyncio.wait_for(loop, 5)
    assert bot.finished == ["a1", "a2", "b1"]
    assert poller.calls == 2  # простаивающий опрос отменён остановкой, нового не было


async def test_polling_pauses_on_backlog_and_resumes(tmp_path: Path) -> None:
    bot = GatedBot()
    names = ["m1", "m2", "m3", "m4"]
    bot.hold(*names)
    first = [chat_update(chat_id, name) for chat_id, name in enumerate(names)]
    poller = ScriptedPoller(first, [chat_update(9, "later")])
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop, queue_limit=4)
    await bot.wait_started("m4")
    await settle()
    assert poller.calls == 1  # очередь полна: MAX не опрашивается, апдейты ждут на его стороне
    bot.release("m1")
    await bot.wait_finished("m1")
    await settle()
    assert poller.calls == 1
    bot.release("m2")

    await bot.wait_finished("later")
    assert poller.calls >= 2
    stop.set()
    bot.release("m3", "m4")
    await asyncio.wait_for(loop, 5)
    assert sorted(bot.finished) == ["later", *names]


async def test_stop_is_not_blocked_by_paused_polling(tmp_path: Path) -> None:
    bot = GatedBot()
    bot.hold("m1", "m2")
    poller = ScriptedPoller([chat_update(1, "m1"), chat_update(2, "m2")])
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop, queue_limit=2)
    await bot.wait_started("m2")
    stop.set()
    bot.release("m1", "m2")

    await asyncio.wait_for(loop, 5)
    assert poller.calls == 1
    assert sorted(bot.finished) == ["m1", "m2"]


async def test_stop_gives_up_after_timeout_and_logs_the_loss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot = GatedBot()
    bot.hold("a1")  # ворота не откроются
    poller = ScriptedPoller([chat_update(1, "a1"), chat_update(1, "a2")])
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop, shutdown_timeout_s=0.05)
    await bot.wait_started("a1")
    stop.set()
    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.dispatch"):
        await asyncio.wait_for(loop, 5)

    assert bot.cancelled == ["a1"] and bot.finished == []
    assert [r.getMessage() for r in caplog.records] == [
        "shutdown timeout 0.05 s: 2 updates lost (1 cancelled in progress, 1 never started)"
    ]


async def test_cancelled_loop_does_not_leave_updates_running(tmp_path: Path) -> None:
    """Ctrl+C: раннер отменён — обработки отменяются вместе с ним, а не живут после закрытия
    клиентов MAX и моделей."""
    bot = GatedBot()
    bot.hold("a1")
    poller = ScriptedPoller([chat_update(1, "a1")])

    loop = start_loop(poller, bot, tmp_path, asyncio.Event())
    await bot.wait_started("a1")
    loop.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, 5)
    assert bot.cancelled == ["a1"]


async def test_batch_is_processed_even_if_marker_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Диск не дал записать marker — батч уже получен, и MAX его второй раз не отдаст: бросить
    его значило бы потерять сообщения."""

    def broken(path: Path, marker: int | None) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_save_marker", broken)
    bot = GatedBot()
    poller = ScriptedPoller([chat_update(1, "a1")])
    stop = asyncio.Event()

    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.runner"):
        loop = start_loop(poller, bot, tmp_path, stop)
        await bot.wait_finished("a1")
        stop.set()
        await asyncio.wait_for(loop, 5)

    assert [r.getMessage() for r in caplog.records] == ["marker not saved"]


def test_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dispatch_limits(Settings(_env_file=None)) == DispatchLimits(
        concurrency=16, queue_limit=500, shutdown_timeout_s=120.0
    )
    monkeypatch.setenv("UPDATE_CONCURRENCY", "1")
    monkeypatch.setenv("UPDATE_QUEUE_LIMIT", "50")
    monkeypatch.setenv("SHUTDOWN_TIMEOUT_S", "30")
    assert dispatch_limits(Settings(_env_file=None)) == DispatchLimits(
        concurrency=1, queue_limit=50, shutdown_timeout_s=30.0
    )


@pytest.mark.parametrize("name", ["UPDATE_CONCURRENCY", "UPDATE_QUEUE_LIMIT", "SHUTDOWN_TIMEOUT_S"])
def test_bad_limit_stops_the_bot_at_start(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "-1")
    with pytest.raises(ValueError, match=name.lower()):
        Settings(_env_file=None)


async def test_runner_gives_poll_loop_the_limits_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[Any] = []

    class FakeResource:
        def __init__(self, *_args: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeResource":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def me(self) -> dict[str, Any]:
            return {"name": "bot"}

    async def no_polling(*args: Any) -> None:
        captured.extend(args)

    monkeypatch.setattr(runner, "MaxClient", FakeResource)
    monkeypatch.setattr(runner, "make_llm", FakeResource)
    monkeypatch.setattr(runner, "_poll_loop", no_polling)
    monkeypatch.setattr(runner, "_install_stop_handler", lambda *_args: None)
    settings = Settings(
        _env_file=None,
        max_token="token",
        events_path=str(tmp_path / "events.jsonl"),
        photos_ttl_days=0,
        kb_photos_ttl_days=0,
        update_concurrency=4,
    )

    await runner.run_polling(settings)

    assert captured[-1] == DispatchLimits(concurrency=4, queue_limit=500, shutdown_timeout_s=120.0)


# --- события параллельных чатов: у каждого свой trace_id и свой пользователь ---


class FakeMaxWithPhoto(FakeMax):
    async def download(self, url: str) -> bytes:
        return make_image()


class LockstepLLM:
    """Модель, у которой вызовы двух чатов идут строго парами: никто не получит ответ, пока
    второй чат не дошёл до того же шага. Так обработки гарантированно пересекаются на каждом
    вызове модели — там, где журнал берёт пользователя и trace_id из контекста."""

    def __init__(self) -> None:
        self._pair = asyncio.Barrier(2)

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        await self._pair.wait()
        return LLMResult(
            content=TRANSCRIPT, model=model, tokens_in=9000, tokens_out=150, latency_s=2.0
        )

    async def chat(
        self, messages: list[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        step, _version = _step.get()
        await self._pair.wait()
        content = STRUCTURED if step == "vision_structure" else json.dumps({"items": []})
        return LLMResult(content=content, model=model, tokens_in=500, tokens_out=200, latency_s=1.0)


def photo_update(chat_id: int, user_id: int) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": {"chat_id": chat_id},
                "body": {
                    "mid": f"m{chat_id}",
                    "attachments": [{"type": "image", "payload": {"url": "https://files/1.jpg"}}],
                },
            },
        }
    )


async def test_events_of_parallel_chats_keep_their_own_trace_and_user(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    regular, tester = anonymize(42), anonymize(43)
    assert regular is not None and tester is not None
    log = EventLog(path, "prod", test_users={tester})
    bot = Bot(
        FakeMaxWithPhoto(),  # type: ignore[arg-type]
        JournaledLLM(LockstepLLM(), log),  # type: ignore[arg-type]
        InMemoryStateStore(),
        log,
        Settings(_env_file=None, photos_ttl_days=0),
    )
    poller = ScriptedPoller([photo_update(7, 42), photo_update(8, 43)])
    stop = asyncio.Event()

    loop = start_loop(poller, bot, tmp_path, stop)
    await asyncio.wait_for(poller.idle.wait(), 5)
    stop.set()
    await asyncio.wait_for(loop, 5)

    # строки журнала целые: записи параллельных обработок не перемешались внутри строки
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    by_user: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_user.setdefault(event["user"], []).append(event)
    assert set(by_user) == {regular, tester}
    traces = {user: {e["trace_id"] for e in rows} for user, rows in by_user.items()}
    assert all(len(ids) == 1 and None not in ids for ids in traces.values())
    assert traces[tester] != traces[regular]
    for rows in by_user.values():
        calls = [e["step"] for e in rows if e["type"] == "llm_call"]
        assert calls == ["vision", "vision_structure", "praise"]
        assert "task_checked" in [e["type"] for e in rows]
    # тестовый трафик отделён по пользователю: ни одно событие тестера не ушло в зачёт
    assert {e["env"] for e in by_user[tester]} == {"test"}
    assert {e["env"] for e in by_user[regular]} == {"prod"}
    # обработки и правда шли одновременно: вызовы модели двух чатов чередуются
    order = [e["user"] for e in events if e["type"] == "llm_call"]
    assert sorted(order[:2]) == sorted([regular, tester])
