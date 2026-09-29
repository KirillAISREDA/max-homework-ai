"""Нагрузочный тест: поток сообщений виртуальных семей, замеры по шагам нагрузки."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.handlers import RETRY
from hwcheck.bot.models import MaxUpdate
from hwcheck.loadtest import (
    LoadMax,
    Sample,
    Step,
    StepResult,
    VirtualUsers,
    parse_steps,
    percentile,
    render_report,
    run_load,
    summarize_step,
)

PHOTOS = [b"photo-0", b"photo-1"]


def test_steps_are_parsed_from_command_line() -> None:
    assert parse_steps("1:60,2.5:30") == [Step(rps=1, seconds=60), Step(rps=2.5, seconds=30)]
    for bad in ("", "10", "0:60", "5:0", "a:b", "-1:10"):
        with pytest.raises(ValueError, match="шаг"):
            parse_steps(bad)


def test_percentile_of_small_samples() -> None:
    assert percentile([], 95) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([float(n) for n in range(1, 101)], 95) == pytest.approx(95.05)


async def test_load_max_serves_photos_and_remembers_answers() -> None:
    load_max = LoadMax(PHOTOS)
    assert await load_max.download("load://photo/1") == b"photo-1"
    assert await load_max.download("load://photo/7") == b"photo-1"  # по кругу
    await load_max.send_message(5, "Проверил!", buttons=[[{"payload": "tutor:0", "text": "x"}]])
    await load_max.send_message(5, RETRY)
    await load_max.answer_callback("cb")
    assert await load_max.upload_image(b"crop") == "load-image"
    assert load_max.last_buttons(5) == []
    assert load_max.take_failure(5) is True and load_max.take_failure(5) is False
    assert load_max.replies == 2


def kinds(updates: list[MaxUpdate]) -> list[str]:
    return [
        "photo"
        if u.message is not None and u.message.image_urls
        else "button"
        if u.callback is not None
        else "text"
        for u in updates
    ]


async def test_virtual_family_follows_the_scenario() -> None:
    """Домашка → «Разобрать» → ответы тьютору → следующая домашка: как живая семья."""
    load_max = LoadMax(PHOTOS)
    users = VirtualUsers(load_max, tutor_answers=2)

    first = users.next_update()
    chat_id = first.effective_chat_id
    assert chat_id is not None and kinds([first]) == ["photo"]
    # пока бот не ответил, эта семья молчит: следующее сообщение — от новой
    second = users.next_update()
    assert second.effective_chat_id != chat_id and kinds([second]) == ["photo"]

    buttons = [[{"type": "callback", "text": "Разобрать №1", "payload": "tutor:0"}]]
    await load_max.send_message(chat_id, "Проверил! 0 из 1 верно.", buttons=buttons)
    users.done(chat_id)
    pressed = users.next_update()
    assert pressed.effective_chat_id == chat_id and pressed.callback is not None
    assert pressed.callback.payload == "tutor:0"

    await load_max.send_message(chat_id, "Сложи сначала единицы")
    users.done(chat_id)
    script = []
    for _ in range(3):
        update = users.next_update()
        assert update.effective_chat_id == chat_id
        script.append(update)
        await load_max.send_message(chat_id, "Подумай ещё")
        users.done(chat_id)
    assert kinds(script) == ["text", "text", "photo"]  # два ответа тьютору и новая домашка
    assert users.count == 2


def sample(kind: str, queued: float, started: float, finished: float, ok: bool = True) -> Sample:
    return Sample(kind=kind, queued_at=queued, started_at=started, finished_at=finished, ok=ok)


def test_step_summary_counts_latency_throughput_and_tokens() -> None:
    samples = [
        sample("photo", 0.0, 0.5, 30.5),
        sample("photo", 1.0, 1.0, 41.0, ok=False),
        sample("button", 2.0, 2.0, 2.2),
        sample("text", 3.0, 3.1, 8.1),
    ]
    calls: list[dict[str, Any]] = [
        {"ts": 5.0, "tokens_in": 1000, "tokens_out": 200, "cost": 1.5, "status": "ok"},
        {"ts": 6.0, "tokens_in": 500, "tokens_out": 100, "cost": None, "status": "error"},
        {"ts": 500.0, "tokens_in": 9, "tokens_out": 9, "cost": 9, "status": "ok"},  # другой шаг
    ]
    result = summarize_step(
        Step(rps=2, seconds=10), samples, calls, started=0.0, finished=60.0, sent=5, peak=3
    )
    assert (result.sent, result.completed, result.failed, result.unfinished) == (5, 4, 1, 1)
    assert result.throughput == pytest.approx(4 / 60)
    assert result.latency["photo"].p50 == pytest.approx(35.25)
    assert result.latency["button"].p95 == pytest.approx(0.2)
    assert result.queue_wait_p95 == pytest.approx(0.44, abs=0.01)
    assert (result.llm_calls, result.llm_errors, result.tokens) == (2, 1, 1800)
    assert result.tokens_per_minute == pytest.approx(1800)
    assert result.cost == pytest.approx(1.5)
    assert result.peak_pending == 3


def test_report_is_a_table_with_every_step() -> None:
    steps = [
        summarize_step(Step(rps, 60), [sample("photo", 0, 0, 20)], [], 0.0, 60.0, sent=1, peak=1)
        for rps in (1, 10)
    ]
    text = render_report(steps, settings_note="vision=gw:x, параллельно 16")
    assert "| 1 |" in text and "| 10 |" in text
    assert "vision=gw:x" in text and "p95" in text
    assert isinstance(steps[0], StepResult)


async def test_run_load_sends_at_rate_and_waits_for_answers(tmp_path: Path) -> None:
    load_max = LoadMax(PHOTOS)
    handled: list[int] = []

    async def bot(update: MaxUpdate) -> None:
        chat_id = update.effective_chat_id
        assert chat_id is not None
        handled.append(chat_id)
        await load_max.send_message(chat_id, "Проверил! 1 из 1 верно.")

    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"ts": 0.0, "type": "llm_call", "tokens_in": 1, "tokens_out": 1}) + "\n",
        encoding="utf-8",
    )
    results = await run_load(
        bot, load_max, [Step(rps=50, seconds=0.2), Step(rps=100, seconds=0.1)], events_path=events
    )
    assert [r.step.rps for r in results] == [50, 100]
    assert [r.sent for r in results] == [10, 10]
    assert all(r.completed == r.sent and r.failed == 0 and r.unfinished == 0 for r in results)
    assert len(handled) == 20
    assert results[0].llm_calls == 0  # запись журнала до начала шага в него не входит


async def test_unanswered_step_does_not_leak_into_the_next(tmp_path: Path) -> None:
    """Шаг, который бот не успел разобрать, отменяется: хвост не попадает в замеры следующего."""
    load_max = LoadMax(PHOTOS)
    slow = True

    async def bot(update: MaxUpdate) -> None:
        chat_id = update.effective_chat_id
        assert chat_id is not None
        if slow:
            await asyncio.sleep(30)
        await load_max.send_message(chat_id, "Проверил! 1 из 1 верно.")

    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    steps = [Step(rps=50, seconds=0.1), Step(rps=50, seconds=0.1)]
    task = asyncio.ensure_future(
        run_load(bot, load_max, steps, events_path=events, drain_timeout_s=0.2)
    )
    await asyncio.sleep(0.25)
    slow = False  # второй шаг бот разбирает сразу
    first, second = await task
    assert (first.sent, first.completed, first.unfinished) == (5, 0, 5)
    assert (second.sent, second.completed, second.unfinished) == (5, 5, 0)


def test_report_names_peak_memory() -> None:
    step = summarize_step(Step(1, 60), [sample("photo", 0, 0, 20)], [], 0.0, 60.0, sent=1, peak=1)
    text = render_report([step], settings_note="x", peak_memory_mb=412.4)
    assert "Пик памяти процесса: 412 МБ" in text
    assert "Пик памяти" not in render_report([step], settings_note="x")
