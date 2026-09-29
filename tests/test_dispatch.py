"""Диспетчер апдейтов (bot/dispatch.py): чаты — параллельно, апдейты одного чата — по порядку.

Задержки управляются событиями (`GatedBot`), настоящих пауз нет: тест не зависит от скорости
машины.
"""

import asyncio
import logging
from contextvars import ContextVar
from typing import Any

import pytest

from conftest import GatedBot, chat_update, settle
from hwcheck.bot.dispatch import DispatchLimits, UpdateDispatcher
from hwcheck.bot.models import MaxUpdate


def make(bot: GatedBot, **limits: Any) -> UpdateDispatcher:
    return UpdateDispatcher(bot.handle_update, DispatchLimits(**limits))


async def test_slow_chat_does_not_delay_another_chat() -> None:
    bot = GatedBot()
    bot.hold("a1")
    dispatcher = make(bot)

    dispatcher.submit(chat_update(1, "a1"))
    dispatcher.submit(chat_update(2, "b1"))

    await bot.wait_finished("b1")
    assert bot.started == ["a1", "b1"]
    assert bot.finished == ["b1"]  # чат A всё ещё проверяет домашку
    bot.release("a1")
    assert await dispatcher.drain() == 0
    assert bot.finished == ["b1", "a1"]


async def test_updates_of_one_chat_go_in_order_even_if_first_is_slow() -> None:
    bot = GatedBot()
    bot.hold("a1")
    dispatcher = make(bot)

    for name in ("a1", "a2", "a3"):
        dispatcher.submit(chat_update(1, name))

    await bot.wait_started("a1")
    await settle()
    # свободных мест хватает, но состояние диалога одно: второй апдейт ждёт первого
    assert bot.started == ["a1"]
    bot.release("a1")
    assert await dispatcher.drain() == 0
    assert bot.started == bot.finished == ["a1", "a2", "a3"]
    assert bot.peak == 1


async def test_concurrency_limit_is_kept() -> None:
    bot = GatedBot()
    names = [f"chat{n}" for n in range(5)]
    bot.hold(*names)
    dispatcher = make(bot, concurrency=2)

    for chat_id, name in enumerate(names):
        dispatcher.submit(chat_update(chat_id, name))

    await settle()
    assert bot.started == names[:2]
    assert (dispatcher.running, dispatcher.pending) == (2, 5)
    bot.release(*names)
    assert await dispatcher.drain() == 0
    assert sorted(bot.finished) == names
    assert bot.peak == 2


async def test_concurrency_one_is_strictly_sequential_in_arrival_order() -> None:
    """`UPDATE_CONCURRENCY=1` — выключатель: по одному и в порядке прихода, как до диспетчера."""
    bot = GatedBot()
    dispatcher = make(bot, concurrency=1)
    arrival = [(1, "a1"), (1, "a2"), (2, "b1"), (None, "service"), (1, "a3"), (2, "b2")]

    for chat_id, name in arrival:
        dispatcher.submit(chat_update(chat_id, name))

    assert await dispatcher.drain() == 0
    assert bot.started == bot.finished == [name for _chat, name in arrival]
    assert bot.peak == 1


async def test_updates_without_chat_share_one_queue() -> None:
    bot = GatedBot()
    bot.hold("first")
    dispatcher = make(bot)

    dispatcher.submit(chat_update(None, "first"))
    dispatcher.submit(chat_update(None, "second"))
    dispatcher.submit(chat_update(1, "a1"))

    await bot.wait_finished("a1")
    await settle()
    assert bot.started == ["first", "a1"]  # второй служебный ждёт первого, чат — нет
    assert dispatcher.chats == 1
    bot.release("first")
    assert await dispatcher.drain() == 0
    assert bot.finished == ["a1", "first", "second"]


async def test_failed_update_stops_neither_its_chat_nor_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = GatedBot()
    bot.failing.add("a1")
    dispatcher = make(bot)

    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.dispatch"):
        for chat_id, name in ((1, "a1"), (1, "a2"), (2, "b1")):
            dispatcher.submit(chat_update(chat_id, name))
        assert await dispatcher.drain() == 0

    assert sorted(bot.finished) == ["a2", "b1"]
    [record] = caplog.records
    assert record.getMessage() == "update failed: message_created"
    assert record.exc_info is not None
    assert (dispatcher.pending, dispatcher.chats) == (0, 0)


async def test_update_cancelled_from_inside_does_not_kill_queue_of_its_chat() -> None:
    """Отмена из глубины обработки (не по остановке бота) — сбой одного апдейта: иначе задача
    очереди умерла бы, а следующие сообщения этого чата копились бы без обработки."""
    bot = GatedBot()
    bot.self_cancelling.add("a1")
    dispatcher = make(bot)

    dispatcher.submit(chat_update(1, "a1"))
    dispatcher.submit(chat_update(1, "a2"))

    assert await dispatcher.drain() == 0
    assert bot.finished == ["a2"]
    assert (dispatcher.pending, dispatcher.chats) == (0, 0)


async def test_chat_queues_are_dropped_when_empty() -> None:
    bot = GatedBot()
    bot.hold("slow")
    dispatcher = make(bot)

    dispatcher.submit(chat_update(0, "slow"))
    for chat_id in range(1, 201):
        dispatcher.submit(chat_update(chat_id, f"m{chat_id}-1"))
        dispatcher.submit(chat_update(chat_id, f"m{chat_id}-2"))
    await bot.wait_finished("m200-2")
    await settle()

    # двести чатов обработаны и забыты: остался только тот, что ещё работает
    assert (dispatcher.chats, dispatcher.pending, dispatcher.running) == (1, 1, 1)
    bot.release("slow")
    assert await dispatcher.drain() == 0
    assert (dispatcher.chats, dispatcher.pending, dispatcher.running) == (0, 0, 0)
    assert len(bot.finished) == 401


async def test_chat_gets_new_queue_after_the_old_one_is_dropped() -> None:
    bot = GatedBot()
    dispatcher = make(bot)

    dispatcher.submit(chat_update(1, "a1"))
    await bot.wait_finished("a1")
    await settle()
    assert dispatcher.chats == 0
    dispatcher.submit(chat_update(1, "a2"))

    assert await dispatcher.drain() == 0
    assert bot.finished == ["a1", "a2"]


async def test_backpressure_pauses_until_queue_is_half_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = GatedBot()
    names = ["m1", "m2", "m3", "m4"]
    bot.hold(*names)
    dispatcher = make(bot, queue_limit=4)

    with caplog.at_level(logging.INFO, logger="hwcheck.bot.dispatch"):
        for chat_id, name in enumerate(names[:3]):
            dispatcher.submit(chat_update(chat_id, name))
        assert dispatcher.has_capacity
        await asyncio.wait_for(dispatcher.wait_for_capacity(), 5)  # места есть — не ждём
        dispatcher.submit(chat_update(3, names[3]))
        assert not dispatcher.has_capacity

        waiting = asyncio.create_task(dispatcher.wait_for_capacity())
        bot.release("m1")
        await bot.wait_finished("m1")
        await settle()
        # 3 из 4: опрос не возобновляется от первого же освободившегося места, иначе бот
        # метался бы между паузой и опросом на каждом апдейте
        assert not waiting.done()
        bot.release("m2")
        await asyncio.wait_for(waiting, 5)
        assert dispatcher.has_capacity and dispatcher.pending == 2

    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == "updates backlog 4 >= 4: polling paused"
    assert messages[1].startswith("updates backlog 2: polling resumed after ")
    assert caplog.records[0].levelno == logging.WARNING
    bot.release("m3", "m4")
    assert await dispatcher.drain() == 0


async def test_drain_waits_for_accepted_updates() -> None:
    bot = GatedBot()
    bot.hold("a1")
    dispatcher = make(bot, concurrency=1)
    for chat_id, name in ((1, "a1"), (1, "a2"), (2, "b1")):
        dispatcher.submit(chat_update(chat_id, name))

    draining = asyncio.create_task(dispatcher.drain())
    await bot.wait_started("a1")
    await settle()
    assert not draining.done()  # и начатый, и ещё не начатые апдейты — принятые
    bot.release("a1")

    assert await asyncio.wait_for(draining, 5) == 0
    assert bot.finished == ["a1", "a2", "b1"]


async def test_drain_timeout_cancels_the_rest_and_logs_the_loss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = GatedBot()
    bot.hold("a1", "b1")  # ворота не откроются: модель «зависла»
    dispatcher = make(bot, shutdown_timeout_s=0.05)
    for chat_id, name in ((1, "a1"), (1, "a2"), (2, "b1"), (3, "c1")):
        dispatcher.submit(chat_update(chat_id, name))
    await bot.wait_finished("c1")

    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.dispatch"):
        lost = await asyncio.wait_for(dispatcher.drain(), 5)

    assert lost == 3
    assert sorted(bot.cancelled) == ["a1", "b1"]
    assert "a2" not in bot.started
    assert [r.getMessage() for r in caplog.records] == [
        "shutdown timeout 0.05 s: 3 updates lost (2 cancelled in progress, 1 never started)"
    ]
    assert (dispatcher.chats, dispatcher.pending, dispatcher.running) == (0, 0, 0)


async def test_abort_cancels_everything_at_once() -> None:
    """Выход по исключению или Ctrl+C: обработки не должны пережить закрытие клиентов."""
    bot = GatedBot()
    bot.hold("a1")
    dispatcher = make(bot)
    dispatcher.submit(chat_update(1, "a1"))
    dispatcher.submit(chat_update(1, "a2"))
    await bot.wait_started("a1")

    await asyncio.wait_for(dispatcher.abort(), 5)

    assert bot.cancelled == ["a1"]
    assert (dispatcher.chats, dispatcher.pending, dispatcher.running) == (0, 0, 0)


async def test_abort_before_queue_started_leaves_nothing_behind() -> None:
    bot = GatedBot()
    dispatcher = make(bot)
    dispatcher.submit(chat_update(1, "a1"))  # задача очереди создана, но ещё не сделала ни шага

    await asyncio.wait_for(dispatcher.abort(), 5)

    assert bot.started == []
    assert (dispatcher.chats, dispatcher.pending, dispatcher.running) == (0, 0, 0)
    with pytest.raises(RuntimeError):
        dispatcher.submit(chat_update(1, "a2"))


async def test_every_update_runs_in_its_own_context() -> None:
    """Переменная контекста, забытая одной обработкой, не видна следующей — даже в том же чате:
    на контексте держатся trace_id и пользователь журнала событий."""
    leaked: ContextVar[str | None] = ContextVar("leaked", default=None)
    seen: list[str | None] = []

    async def handler(update: MaxUpdate) -> None:
        seen.append(leaked.get())
        leaked.set(update.update_type)  # без reset — как забытый выход из блока trace()
        await asyncio.sleep(0)

    dispatcher = UpdateDispatcher(handler, DispatchLimits())
    for chat_id in (1, 1, 2, None):
        dispatcher.submit(chat_update(chat_id, "message_created"))

    assert await dispatcher.drain() == 0
    assert seen == [None, None, None, None]
    assert leaked.get() is None


@pytest.mark.parametrize(
    "limits",
    [{"concurrency": 0}, {"queue_limit": 0}, {"shutdown_timeout_s": -1.0}],
)
def test_limits_are_validated(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DispatchLimits(**limits)
