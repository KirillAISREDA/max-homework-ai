"""Диспетчер апдейтов: разные чаты обрабатываются параллельно, один чат — строго по порядку.

Проверка домашки — 30–60 с вызовов моделей. При обработке по одному десятая семья в 19:00 ждала
бы минуты, а нажатие кнопки любого пользователя — конца чужой проверки.

Правила:

- у каждого чата своя очередь и своя задача: состояние диалога (`bot/fsm.py`) читается и пишется
  без блокировок, два апдейта одного чата одновременно затёрли бы записи друг друга;
- апдейты без чата идут в одну общую очередь;
- одновременных обработок не больше `concurrency`; при `concurrency=1` очередь одна на всех —
  апдейты идут по одному в порядке прихода, как до диспетчера (аварийный выключатель);
- каждый апдейт обрабатывается в своей задаче, то есть в своём контексте: trace_id и пользователь
  журнала событий (`events.trace`) не попадают в события чужой обработки;
- очередь чата живёт, пока в ней есть работа: иначе структуры росли бы с каждым новым чатом;
- необработанных апдейтов не больше `queue_limit` (плюс один батч): дальше раннер не опрашивает
  MAX, пока очередь не разгрузится наполовину, — апдейты ждут на стороне MAX, а не в памяти.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from hwcheck.bot.models import MaxUpdate

logger = logging.getLogger(__name__)

Handler = Callable[[MaxUpdate], Coroutine[Any, Any, None]]
QueueKey = int | None  # chat_id; None — общая очередь апдейтов без чата
# после отмены обработке нужно время закрыть начатое (запись о вызове модели в журнал, удаление
# фото из хранилища GigaChat). Дольше ждать нельзя: после срока остановки до SIGKILL от Docker
# остаётся `stop_grace_period − SHUTDOWN_TIMEOUT_S`
CANCEL_GRACE_S = 10.0


@dataclass(frozen=True)
class DispatchLimits:
    concurrency: int = 8  # одновременных обработок; 1 — по одному, как до диспетчера
    queue_limit: int = 100  # необработанных апдейтов, после которых опрос MAX ждёт
    shutdown_timeout_s: float = 100.0  # срок дообработки при остановке

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency: нужна хотя бы одна одновременная обработка")
        if self.queue_limit < 1:
            raise ValueError("queue_limit: очередь должна вмещать хотя бы один апдейт")
        if self.shutdown_timeout_s < 0:
            raise ValueError("shutdown_timeout_s: срок дообработки не может быть отрицательным")

    @property
    def resume_at(self) -> int:
        """Опрос возобновляется, когда очередь разгрузилась наполовину, а не от первого
        освободившегося места: иначе бот метался бы между паузой и опросом на каждом апдейте."""
        return self.queue_limit // 2


@dataclass
class _Chat:
    queue: deque[MaxUpdate]
    worker: asyncio.Task[None]


class UpdateDispatcher:
    def __init__(self, handler: Handler, limits: DispatchLimits) -> None:
        self._handler = handler
        self._limits = limits
        self._slots = asyncio.Semaphore(limits.concurrency)
        self._chats: dict[QueueKey, _Chat] = {}
        self._pending = 0  # принятые и не законченные: в очередях и в обработке
        self._running = 0  # из них в обработке
        self._stopped = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._capacity = asyncio.Event()
        self._capacity.set()

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def running(self) -> int:
        return self._running

    @property
    def chats(self) -> int:
        """Живых очередей; растёт с нагрузкой, а не с числом чатов, писавших боту когда-либо."""
        return len(self._chats)

    @property
    def has_capacity(self) -> bool:
        return self._capacity.is_set()

    def submit(self, update: MaxUpdate) -> None:
        """Ставит апдейт в очередь его чата и сразу возвращается: опрос MAX обработки не ждёт."""
        if self._stopped:
            raise RuntimeError("dispatcher is stopped")
        # одна обработка за раз — значит, и очередь одна: порядок прихода сохраняется и между
        # чатами, как было до диспетчера
        key = update.effective_chat_id if self._limits.concurrency > 1 else None
        chat = self._chats.get(key)
        if chat is None:
            queue: deque[MaxUpdate] = deque()
            chat = _Chat(queue, asyncio.create_task(self._run_chat(key, queue)))
            self._chats[key] = chat
        chat.queue.append(update)
        self._pending += 1
        self._idle.clear()
        if self._pending >= self._limits.queue_limit:
            self._capacity.clear()

    async def wait_for_capacity(self) -> None:
        """Обратное давление: возвращается, когда в очередях есть место для нового батча."""
        if self._capacity.is_set():
            return
        logger.warning(
            "updates backlog %d >= %d: polling paused", self._pending, self._limits.queue_limit
        )
        paused_at = time.monotonic()
        await self._capacity.wait()
        logger.info(
            "updates backlog %d: polling resumed after %.1f s",
            self._pending,
            time.monotonic() - paused_at,
        )

    async def drain(self) -> int:
        """Остановка: ждёт, пока принятые апдейты дообработаются, но не дольше срока. Что не
        успело — отменяется; возвращает число потерянных апдейтов (они же — в логе)."""
        if self._pending == 0:
            return 0
        timeout = self._limits.shutdown_timeout_s
        logger.info("stopping: %d accepted updates to finish in %g s", self._pending, timeout)
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            lost, running = self._pending, self._running
            logger.error(
                "shutdown timeout %g s: %d updates lost "
                "(%d cancelled in progress, %d never started)",
                timeout,
                lost,
                running,
                lost - running,
            )
            await self.abort()
            return lost
        return 0

    async def abort(self) -> None:
        """Отменяет всё принятое. Нужен и при выходе по исключению или Ctrl+C: обработки не
        должны пережить раннер — дальше закрываются клиенты MAX, моделей и базы."""
        self._stopped = True
        workers = [chat.worker for chat in self._chats.values()]
        if not workers:
            return
        for worker in workers:
            worker.cancel()
        _done, stuck = await asyncio.wait(workers, timeout=CANCEL_GRACE_S)
        if stuck:
            logger.error(
                "%d chat queues still running %g s after cancel", len(stuck), CANCEL_GRACE_S
            )
        for key, chat in list(self._chats.items()):
            if chat.worker.done():
                # задача отменена до первого шага: её `finally` не выполнялся, убираем сами
                del self._chats[key]
                self._settle(len(chat.queue))

    async def _run_chat(self, key: QueueKey, queue: deque[MaxUpdate]) -> None:
        try:
            # `_stopped`: обработка могла проглотить отмену — следующий апдейт уже не начинаем
            while queue and not self._stopped:
                async with self._slots:
                    # из очереди — только с местом в руках: ждущие места считаются не начатыми
                    update = queue.popleft()
                    self._running += 1
                    try:
                        await self._process(update)
                    finally:
                        self._running -= 1
                        self._settle(1)
        finally:
            # между проверкой `while queue` и этим удалением нет ни одного await: апдейт не
            # может попасть в очередь, которую уже некому разбирать
            del self._chats[key]
            self._settle(len(queue))  # не ноль только при отмене

    async def _process(self, update: MaxUpdate) -> None:
        # своя задача — свой контекст: копия контекста очереди, в который обработки не пишут.
        # Отмена очереди (остановка) уходит в обработку через это же ожидание
        work = asyncio.create_task(self._handler(update))
        try:
            await work
        except asyncio.CancelledError:
            if _cancel_requested():
                raise
            # отмена пришла из глубины обработки, а не от остановки: это сбой одного апдейта,
            # очередь чата должна жить — иначе его следующие сообщения копились бы без разбора
            logger.error("update cancelled from inside: %s", update.update_type)
        except Exception:
            # один сбойный апдейт не роняет ни очередь своего чата, ни чужие
            logger.exception("update failed: %s", update.update_type)

    def _settle(self, count: int) -> None:
        self._pending -= count
        if self._pending <= self._limits.resume_at:
            self._capacity.set()
        if self._pending == 0:
            self._idle.set()


def _cancel_requested() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
