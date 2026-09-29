"""Отчёт родителю раз в неделю: расписание и цикл рассылки внутри процесса бота.

Слот — день недели и час по московскому времени (по умолчанию воскресенье 18:00): часовой
пояс семьи неизвестен. Неделя отчёта — 7 суток до слота, конец в неделю не входит: каждая
домашка попадает ровно в один отчёт.

Цикл — задача asyncio рядом с опросом MAX, без отдельного контейнера и cron: раз в минуту
смотрит, наступил ли слот, и пишет тем, у кого нет отметки этой недели. Отметка ставится до
отправки (db/reports.py): после рестарта отчёт не уходит второй раз. Бот лежал в час слота —
отчёт уйдёт позже, пока не кончилось окно (`catchup`), но не ночью (`quiet`); дальше неделя
пропускается.

Сбой рассылки не трогает бота: исключение тика — событие `weekly_loop_failed` и следующий
тик через минуту, сбой отчёта одному родителю — его отметка `failed` и следующий родитель.
Повторов нет: отчёт, который не ушёл, придёт через неделю или по кнопке.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from hwcheck.bot.onboarding.context import MSK
from hwcheck.bot.report_text import Period
from hwcheck.db.repo import Account
from hwcheck.db.reports import ReportRepository, WeeklyStatus
from hwcheck.events import EventLog, trace

if TYPE_CHECKING:
    # отчёт сам берёт отсюда период недели: при выполнении импорт был бы кольцом
    from hwcheck.bot.report import ParentReporter

logger = logging.getLogger(__name__)

WEEK = timedelta(days=7)
TICK_S = 60.0
# между родителями: MAX ограничивает частоту сообщений бота, общий предел рассылки неизвестен
PAUSE_S = 0.5
# клиент MAX ждёт ответа до 100 с (long polling): зависший запрос задержал бы всех остальных
SEND_TIMEOUT_S = 20.0

Sleep = Callable[[asyncio.Event, float], Awaitable[None]]


@dataclass(frozen=True)
class WeeklySchedule:
    weekday: int = 6  # 0 — понедельник, 6 — воскресенье
    hour: int = 18  # по московскому времени
    catchup: timedelta = timedelta(hours=27)  # до понедельника 21:00
    quiet: tuple[int, int] = (22, 9)  # с 22:00 до 09:00 не пишем

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday: день недели — от 0 (понедельник) до 6 (воскресенье)")
        if not all(0 <= hour <= 23 for hour in (self.hour, *self.quiet)):
            raise ValueError("hour, quiet: час — от 0 до 23")
        if self.catchup <= timedelta(0):
            raise ValueError("catchup: окно отправки должно быть больше нуля")
        if _is_quiet(self.hour, self.quiet):
            start, end = self.quiet
            raise ValueError(
                f"hour: {self.hour}:00 — тихие часы (с {start}:00 до {end}:00 по Москве), "
                "в это время бот родителям не пишет"
            )


def _is_quiet(hour: int, quiet: tuple[int, int]) -> bool:
    start, end = quiet
    if start > end:  # через полночь: с 22 до 9
        return hour >= start or hour < end
    return start <= hour < end


def _moscow(moment: datetime) -> datetime:
    """Время без пояса сдвинуло бы слот на три часа молча — лучше ошибка."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("нужно время с часовым поясом")
    return moment.astimezone(MSK)


def last_slot(now: datetime, schedule: WeeklySchedule) -> datetime:
    """Последний наступивший слот (в UTC); в сам час слота — он."""
    local = _moscow(now)
    days_back = (local.weekday() - schedule.weekday) % 7
    slot = local.replace(hour=schedule.hour, minute=0, second=0, microsecond=0) - timedelta(
        days=days_back
    )
    if slot > local:  # сегодня день слота, но час ещё не наступил
        slot -= WEEK
    return slot.astimezone(UTC)


def next_slot(now: datetime, schedule: WeeklySchedule) -> datetime:
    return last_slot(now, schedule) + WEEK


def deliverable(now: datetime, slot: datetime, schedule: WeeklySchedule) -> bool:
    """Можно ли сейчас слать отчёт недели, кончившейся в `slot`: окно не прошло и не ночь."""
    local, start = _moscow(now), _moscow(slot)
    if not start <= local < start + schedule.catchup:
        return False
    return not _is_quiet(local.hour, schedule.quiet)


def weekly_period(slot: datetime) -> Period:
    """Неделя отчёта: 7 суток до слота. Подпись — календарные дни по Москве, от дня прошлого
    слота до дня этого: «27 сентября – 4 октября»."""
    end = _moscow(slot)
    start = end - WEEK
    return Period(
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
        first_day=start.date(),
        last_day=end.date(),
    )


def previous_period(slot: datetime) -> Period:
    """Неделя перед неделей отчёта: по ней видно, первая ли это неделя без домашек."""
    return weekly_period(slot - WEEK)


async def sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую остановка прерывает сразу: бот не ждёт минуту до выхода."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class WeeklyReportLoop:
    def __init__(
        self,
        reporter: ParentReporter,
        reports: ReportRepository,
        events: EventLog,
        schedule: WeeklySchedule,
        *,
        clock: Callable[[], datetime],
        tick_s: float = TICK_S,
        pause_s: float = PAUSE_S,
        send_timeout_s: float = SEND_TIMEOUT_S,
        sleep: Sleep = sleep_unless_stopped,
    ) -> None:
        self._reporter = reporter
        self._reports = reports
        self._events = events
        self._schedule = schedule
        self._clock = clock
        self._tick_s = tick_s
        self._pause_s = pause_s
        self._send_timeout_s = send_timeout_s
        self._sleep = sleep  # в тестах — сон без ожидания
        # слот, о пропуске которого уже решено: событие — одно на слот, а не на каждый тик.
        # В памяти процесса: после рестарта о пропущенной неделе будет ещё одно событие
        self._missed_checked: datetime | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Тик сразу при старте и дальше раз в `tick_s`, пока нет остановки."""
        while not stop.is_set():
            await self.tick(stop)
            await self._sleep(stop, self._tick_s)

    async def tick(self, stop: asyncio.Event) -> int:
        """Один проход; возвращает, скольким родителям отчёт этой недели обработан. Сбой
        наружу не выходит: рассылка не должна уронить бота."""
        processed = 0
        try:
            # счёт — по одному: сбой посреди рассылки не обнуляет уже обработанных
            async for _status in self._deliver(stop):
                processed += 1
        except Exception as exc:
            self._tick_failed(exc)
        if processed:
            logger.info("weekly report: %d parents processed", processed)
        return processed

    async def _deliver(self, stop: asyncio.Event) -> AsyncIterator[WeeklyStatus]:
        now = self._clock()
        slot = last_slot(now, self._schedule)
        if not deliverable(now, slot, self._schedule):
            await self._note_missed(now, slot)
            return
        first = True
        for parent in await self._reports.due_parents(slot):
            if not first:
                await self._sleep(stop, self._pause_s)
            first = False
            # рассылка идёт долго: пока шли паузы, могла прийти остановка или начаться ночь
            if stop.is_set() or not deliverable(self._clock(), slot, self._schedule):
                return
            if not await self._reports.claim_weekly(parent.id, slot):
                continue  # отметку успел поставить другой процесс бота
            status = await self._send(parent, slot)
            await self._reports.finish_weekly(parent.id, slot, status)
            yield status

    async def _send(self, parent: Account, slot: datetime) -> WeeklyStatus:
        # свой trace_id у каждого отчёта, как у каждого апдейта: события одного отчёта вместе
        with trace():
            try:
                return await asyncio.wait_for(
                    self._reporter.send_weekly(parent, slot), self._send_timeout_s
                )
            except Exception as exc:
                return self._reporter.weekly_failed(parent, exc)

    async def _note_missed(self, now: datetime, slot: datetime) -> None:
        """Окно отправки прошло, а не получившие отчёт остались — бот лежал: событие, одно на
        слот. Ночь внутри окна — не пропуск: утром отчёт ещё уйдёт."""
        if now < slot + self._schedule.catchup or self._missed_checked == slot:
            return
        due = await self._reports.due_parents(slot)
        self._missed_checked = slot
        if due:
            logger.warning("weekly report: slot %s missed", slot.isoformat())
            self._events.log("weekly_slot_missed", component="report")

    def _tick_failed(self, exc: Exception) -> None:
        # текст исключения — с данными запроса: в лог и журнал идёт только класс
        error = type(exc).__name__
        logger.warning("weekly report tick failed: %s", error)
        try:
            self._events.log("weekly_loop_failed", component="report", error=error)
        except Exception as log_exc:
            # журнал тоже может отказать (диск): предупреждение в логе уже есть
            logger.warning("weekly_loop_failed not written: %s", type(log_exc).__name__)
