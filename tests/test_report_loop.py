"""Цикл рассылки отчёта раз в неделю: когда шлёт, кому, что переживает и как останавливается.

Настоящих пауз нет: часы и сон — подставные, сон двигает часы. Слот — воскресенье 20.09.2026
18:00 мск; семьи заведены 16.09, у каждой на этой неделе одна домашка.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import settle
from hwcheck.bot.report import ParentReporter
from hwcheck.bot.report_schedule import WeeklyReportLoop, WeeklySchedule
from hwcheck.db.repo import Account
from onboarding_kit import Clock, Kit, actor
from test_parent_report import check, make_family_kit, young_child
from test_weekly_report import EMPTY_WEEK, HEADER, SLOT, WEEK

PARENTS = (5550010, 5550011, 5550012)
QUIET_FROM = SLOT + timedelta(hours=4)  # воскресенье 22:00 мск
MORNING = SLOT + timedelta(hours=15)  # понедельник 09:00 мск
TOO_LATE = SLOT + timedelta(hours=27)  # понедельник 21:00 мск
SECOND = timedelta(seconds=1)


class Sleeper:
    """Сон без ожидания: записывает, сколько просили спать, и двигает часы."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.naps: list[float] = []
        self.on_nap: Callable[[], None] | None = None

    async def __call__(self, stop: asyncio.Event, seconds: float) -> None:
        self.naps.append(seconds)
        self._clock.now += timedelta(seconds=seconds)
        if self.on_nap is not None:
            self.on_nap()
        await asyncio.sleep(0)


class Flaky:
    """Хранилище, у которого названный метод падает заданное число раз."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failures: dict[str, int] = {}

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        method = getattr(self._inner, name)

        async def call(*args: Any, **kw: Any) -> Any:
            if self.failures.get(name, 0) > 0:
                self.failures[name] -= 1
                raise ConnectionError(f"database is down: {name}{args}")
            return await method(*args, **kw)

        return call


async def families(tmp_path: Path, now: datetime = SLOT) -> Kit:
    kit = make_family_kit(tmp_path)
    for user_id in PARENTS:
        await check(kit, await young_child(kit, 3, user_id), 3, 3, 0, 0)
    kit.clock.now = now
    return kit


def make_loop(kit: Kit, **options: Any) -> tuple[WeeklyReportLoop, Sleeper]:
    sleeper = Sleeper(kit.clock)
    loop = WeeklyReportLoop(
        options.pop("reporter", None) or ParentReporter(kit.ctx, kit.repo),
        options.pop("reports", None) or kit.repo,
        kit.ctx.events,
        options.pop("schedule", None) or WeeklySchedule(),
        clock=kit.clock,
        sleep=sleeper,
        **options,
    )
    return loop, sleeper


def receivers(kit: Kit) -> list[int]:
    return [user_id for user_id, _, _ in kit.max.to_users]


async def statuses(kit: Kit, slot: datetime = SLOT) -> list[str | None]:
    found = []
    for user_id in PARENTS:
        account = await kit.repo.get_account(actor(user_id).user_hash)
        assert account is not None
        found.append(kit.repo.weekly_reports.get((account.id, slot)))
    return found


async def test_nothing_is_sent_before_the_slot(tmp_path: Path) -> None:
    kit = await families(tmp_path, SLOT - SECOND)
    loop, sleeper = make_loop(kit)

    assert await loop.tick(asyncio.Event()) == 0

    assert kit.max.to_users == [] and kit.repo.weekly_reports == {}
    assert kit.events() == [] and sleeper.naps == []


async def test_every_due_parent_gets_one_report_at_the_slot(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, sleeper = make_loop(kit)

    assert await loop.tick(asyncio.Event()) == 3

    assert receivers(kit) == list(PARENTS)
    assert all(text.startswith(f"{HEADER}\n\nРебёнок (3 класс)") for _, text, _ in kit.max.to_users)
    assert await statuses(kit) == ["sent", "sent", "sent"]
    assert sleeper.naps == [0.5, 0.5]  # пауза — между родителями
    assert [row["type"] for row in kit.events()] == ["parent_report_sent"] * 3
    assert len({row["trace_id"] for row in kit.events()}) == 3  # у каждого отчёта свой


async def test_pause_between_parents_is_configured(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, sleeper = make_loop(kit, pause_s=2.0)
    await loop.tick(asyncio.Event())
    assert sleeper.naps == [2.0, 2.0]


async def test_second_tick_sends_nothing(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, _ = make_loop(kit)
    stop = asyncio.Event()

    assert await loop.tick(stop) == 3
    kit.clock.now += timedelta(seconds=60)
    assert await loop.tick(stop) == 0
    assert receivers(kit) == list(PARENTS)


async def test_parent_switched_off_gets_nothing(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    silent = await kit.repo.get_account(actor(PARENTS[1]).user_hash)
    assert silent is not None
    await kit.repo.set_weekly(silent.id, False)
    loop, _ = make_loop(kit)

    assert await loop.tick(asyncio.Event()) == 2
    assert receivers(kit) == [PARENTS[0], PARENTS[2]]
    assert await statuses(kit) == ["sent", None, "sent"]


async def test_week_without_homework_is_marked_as_skipped(tmp_path: Path) -> None:
    kit = await families(tmp_path, SLOT + 2 * WEEK)  # домашки были две недели назад
    loop, _ = make_loop(kit)

    assert await loop.tick(asyncio.Event()) == 3
    assert kit.max.to_users == []
    assert await statuses(kit, SLOT + 2 * WEEK) == ["skipped"] * 3
    assert [row["type"] for row in kit.events()] == ["parent_report_skipped"] * 3
    assert await loop.tick(asyncio.Event()) == 0  # и второй раз семью не смотрим


async def test_stop_in_the_middle_stops_before_the_next_claim(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, _ = make_loop(kit)
    stop = asyncio.Event()
    send = kit.max.send_to_user

    async def send_and_stop(*args: Any, **kw: Any) -> None:
        await send(*args, **kw)
        stop.set()

    kit.max.send_to_user = send_and_stop  # type: ignore[method-assign]

    assert await loop.tick(stop) == 1

    assert receivers(kit) == [PARENTS[0]]
    # начатый отчёт доведён до конца, следующие не отмечены — уйдут после рестарта
    assert await statuses(kit) == ["sent", None, None]


async def test_restart_sends_only_to_those_who_were_not_claimed(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    sent, crashed, waiting = [
        await kit.repo.get_account(actor(user_id).user_hash) for user_id in PARENTS
    ]
    assert sent is not None and crashed is not None and waiting is not None
    assert await kit.repo.claim_weekly(sent.id, SLOT)
    await kit.repo.finish_weekly(sent.id, SLOT, "sent")
    assert await kit.repo.claim_weekly(crashed.id, SLOT)  # процесс упал между отметкой и отправкой

    loop, _ = make_loop(kit)  # новый процесс: в памяти ничего, всё — в базе
    assert await loop.tick(asyncio.Event()) == 1

    assert receivers(kit) == [PARENTS[2]]
    # отчёт упавшего потерян, но не отправлен дважды
    assert await statuses(kit) == ["sent", "sending", "sent"]


async def test_storage_failure_is_journalled_and_next_tick_works(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kit = await families(tmp_path)
    reports = Flaky(kit.repo)
    reports.failures["due_parents"] = 1
    loop, _ = make_loop(kit, reports=reports)
    stop = asyncio.Event()

    with caplog.at_level(logging.WARNING, logger="hwcheck.bot.report_schedule"):
        assert await loop.tick(stop) == 0  # исключение наружу не выходит

    [failed] = kit.events()
    assert (failed["type"], failed["error"]) == ("weekly_loop_failed", "ConnectionError")
    assert (failed["component"], failed["user_initiated"]) == ("report", False)
    assert failed["user"] is None
    assert [record.getMessage() for record in caplog.records] == [
        "weekly report tick failed: ConnectionError"
    ]
    assert "database is down" not in caplog.text  # в тексте исключения — данные запроса
    assert await loop.tick(stop) == 3
    assert receivers(kit) == list(PARENTS)


async def test_claim_failure_leaves_the_rest_for_the_next_tick(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    reports = Flaky(kit.repo)
    loop, _ = make_loop(kit, reports=reports)
    stop = asyncio.Event()
    send = kit.max.send_to_user

    async def send_and_break(*args: Any, **kw: Any) -> None:
        await send(*args, **kw)
        reports.failures["claim_weekly"] = 1

    kit.max.send_to_user = send_and_break  # type: ignore[method-assign]
    assert await loop.tick(stop) == 1
    assert await statuses(kit) == ["sent", None, None]
    assert [row["type"] for row in kit.events()] == ["parent_report_sent", "weekly_loop_failed"]

    kit.max.send_to_user = send  # type: ignore[method-assign]
    assert await loop.tick(stop) == 2
    assert receivers(kit) == list(PARENTS)


async def test_unfinished_mark_never_gives_a_second_report(tmp_path: Path) -> None:
    """Отчёт ушёл, а записать это не вышло: отметка остаётся `sending`, повтора нет."""
    kit = await families(tmp_path)
    reports = Flaky(kit.repo)
    reports.failures["finish_weekly"] = 1
    loop, _ = make_loop(kit, reports=reports)
    stop = asyncio.Event()

    await loop.tick(stop)
    assert receivers(kit) == [PARENTS[0]]
    assert [row["type"] for row in kit.events()] == ["parent_report_sent", "weekly_loop_failed"]
    await loop.tick(stop)
    assert receivers(kit) == list(PARENTS)
    assert await statuses(kit) == ["sending", "sent", "sent"]


async def test_journal_failure_does_not_stop_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = await families(tmp_path)
    reports = Flaky(kit.repo)
    reports.failures["due_parents"] = 1
    loop, _ = make_loop(kit, reports=reports)

    def full_disk(*_args: Any, **_kw: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(kit.ctx.events, "log", full_disk)
    assert await loop.tick(asyncio.Event()) == 0
    assert await loop.tick(asyncio.Event()) == 3
    assert await statuses(kit) == ["sent", "sent", "sent"]


async def test_blocked_parent_does_not_stop_the_others(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    kit.max.blocked_users.add(PARENTS[1])
    loop, _ = make_loop(kit)

    assert await loop.tick(asyncio.Event()) == 3

    assert receivers(kit) == [PARENTS[0], PARENTS[2]]
    assert await statuses(kit) == ["sent", "failed", "sent"]
    [failed] = kit.events("parent_report_failed")
    assert (failed["kind"], failed["error"]) == ("weekly", "RuntimeError")
    assert failed["user"] == actor(PARENTS[1]).user_hash
    assert kit.events("weekly_loop_failed") == []
    assert await loop.tick(asyncio.Event()) == 0  # повторов нет


async def test_report_that_raises_does_not_stop_the_others(tmp_path: Path) -> None:
    """Сбой, которого отчёт сам не поймал, — тоже сбой одного родителя, а не всей рассылки."""
    kit = await families(tmp_path)
    unlucky = await kit.repo.get_account(actor(PARENTS[0]).user_hash)
    assert unlucky is not None

    class Raising(ParentReporter):
        async def send_weekly(self, parent: Account, slot: datetime) -> Any:
            if parent.id == unlucky.id:
                raise ZeroDivisionError(f"bug in report of {parent.id}")
            return await super().send_weekly(parent, slot)

    loop, _ = make_loop(kit, reporter=Raising(kit.ctx, kit.repo))
    assert await loop.tick(asyncio.Event()) == 3

    assert receivers(kit) == [PARENTS[1], PARENTS[2]]
    assert await statuses(kit) == ["failed", "sent", "sent"]
    [failed] = kit.events("parent_report_failed")
    assert (failed["kind"], failed["error"]) == ("weekly", "ZeroDivisionError")
    assert failed["user"] == actor(PARENTS[0]).user_hash


async def test_hanging_send_is_cut_by_timeout(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    send = kit.max.send_to_user
    cancelled: list[int] = []

    async def hang_for_second(user_id: int, *args: Any, **kw: Any) -> None:
        if user_id != PARENTS[1]:
            await send(user_id, *args, **kw)
            return
        try:
            await asyncio.Event().wait()  # MAX не отвечает
        except asyncio.CancelledError:
            cancelled.append(user_id)
            raise

    kit.max.send_to_user = hang_for_second  # type: ignore[method-assign]
    loop, _ = make_loop(kit, send_timeout_s=0.01)

    assert await asyncio.wait_for(loop.tick(asyncio.Event()), 5) == 3

    assert receivers(kit) == [PARENTS[0], PARENTS[2]]
    assert cancelled == [PARENTS[1]]  # запрос не остался висеть после срока
    assert await statuses(kit) == ["sent", "failed", "sent"]
    [failed] = kit.events("parent_report_failed")
    assert (failed["kind"], failed["error"]) == ("weekly", "TimeoutError")
    assert failed["user"] == actor(PARENTS[1]).user_hash


async def test_missed_week_is_skipped_and_journalled_once(tmp_path: Path) -> None:
    kit = await families(tmp_path, TOO_LATE - timedelta(minutes=1))  # понедельник 20:59 мск
    loop, _ = make_loop(kit)
    stop = asyncio.Event()
    assert await loop.tick(stop) == 3  # окно ещё открыто
    assert kit.events("weekly_slot_missed") == []

    kit = await families(tmp_path / "late", TOO_LATE)  # бот лежал всё окно: никто не отмечен
    loop, _ = make_loop(kit)
    for _ in range(3):
        assert await loop.tick(stop) == 0
        kit.clock.now += timedelta(seconds=60)

    assert kit.max.to_users == [] and kit.repo.weekly_reports == {}
    [missed] = kit.events()
    assert missed["type"] == "weekly_slot_missed"
    assert (missed["component"], missed["user_initiated"], missed["user"]) == (
        "report", False, None
    )  # fmt: skip

    kit.clock.now = SLOT + WEEK  # следующая неделя идёт как обычно
    assert await loop.tick(stop) == 3
    assert [text for _, text, _ in kit.max.to_users] == [EMPTY_WEEK.replace("14–20", "21–27")] * 3
    assert len(kit.events("weekly_slot_missed")) == 1


async def test_week_that_was_sent_is_not_missed(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, _ = make_loop(kit)
    stop = asyncio.Event()
    assert await loop.tick(stop) == 3

    kit.clock.now = TOO_LATE + timedelta(hours=1)
    assert await loop.tick(stop) == 0
    assert kit.events("weekly_slot_missed") == []


async def test_every_missed_week_is_journalled(tmp_path: Path) -> None:
    kit = await families(tmp_path, TOO_LATE)
    loop, _ = make_loop(kit)
    stop = asyncio.Event()

    await loop.tick(stop)
    kit.clock.now = TOO_LATE + WEEK
    await loop.tick(stop)
    await loop.tick(stop)
    assert len(kit.events("weekly_slot_missed")) == 2


async def test_nothing_is_sent_at_night_and_week_is_caught_up_in_the_morning(
    tmp_path: Path,
) -> None:
    kit = await families(tmp_path, QUIET_FROM + timedelta(minutes=30))  # воскресенье 22:30 мск
    loop, _ = make_loop(kit)
    stop = asyncio.Event()

    assert await loop.tick(stop) == 0
    kit.clock.now = MORNING - timedelta(minutes=1)  # понедельник 08:59 мск
    assert await loop.tick(stop) == 0
    assert kit.max.to_users == [] and kit.repo.weekly_reports == {}
    assert kit.events() == []  # ночь — не пропуск недели

    kit.clock.now = MORNING
    assert await loop.tick(stop) == 3
    assert receivers(kit) == list(PARENTS)
    # отчёт — за ту же неделю, что ушёл бы в воскресенье
    assert all(text.startswith(HEADER) for _, text, _ in kit.max.to_users)
    assert await statuses(kit) == ["sent", "sent", "sent"]


async def test_night_that_begins_during_the_tick_stops_it(tmp_path: Path) -> None:
    kit = await families(tmp_path, QUIET_FROM - timedelta(seconds=0.2))  # 21:59:59,8 мск
    loop, _ = make_loop(kit)
    stop = asyncio.Event()

    assert await loop.tick(stop) == 1  # пауза после первого родителя кончилась уже ночью
    assert receivers(kit) == [PARENTS[0]]
    kit.clock.now = MORNING
    assert await loop.tick(stop) == 2
    assert receivers(kit) == list(PARENTS)


async def test_schedule_of_the_loop_is_configured(tmp_path: Path) -> None:
    kit = await families(tmp_path, SLOT)
    wednesday = WeeklySchedule(weekday=2, hour=9)
    loop, _ = make_loop(
        kit, schedule=wednesday, reporter=ParentReporter(kit.ctx, kit.repo, wednesday)
    )
    stop = asyncio.Event()

    # воскресенье 18:00: последний слот — среда 16.09 09:00, окно догоняющей отправки прошло
    assert await loop.tick(stop) == 0 and kit.max.to_users == []
    kit.clock.now = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)  # среда 23.09, 09:00 мск
    assert await loop.tick(stop) == 3
    [text] = {text for _, text, _ in kit.max.to_users}
    assert text.startswith("📈 Отчёт за неделю: 17–23 сентября\n\n")
    [buttons] = {str(buttons) for _, _, buttons in kit.max.to_users}
    assert "Не присылать по средам" in buttons


# --- run: тики и остановка ---


async def test_run_ticks_at_once_and_then_every_minute(tmp_path: Path) -> None:
    kit = await families(tmp_path, SLOT - timedelta(seconds=90))
    loop, sleeper = make_loop(kit)
    stop = asyncio.Event()
    sent_at: list[datetime] = []
    send = kit.max.send_to_user

    async def send_and_note(*args: Any, **kw: Any) -> None:
        sent_at.append(kit.clock.now)
        await send(*args, **kw)

    def stop_after_the_slot() -> None:
        if kit.clock.now >= SLOT + timedelta(minutes=2):
            stop.set()

    kit.max.send_to_user = send_and_note  # type: ignore[method-assign]
    sleeper.on_nap = stop_after_the_slot
    await asyncio.wait_for(loop.run(stop), 5)

    assert receivers(kit) == list(PARENTS)
    # тики — в 17:58:30, 17:59:30 и 18:00:30: отчёты ушли третьим
    assert sent_at[0] == SLOT + timedelta(seconds=30)
    assert sleeper.naps[:2] == [60.0, 60.0]
    assert sleeper.naps.count(0.5) == 2


async def test_run_does_not_tick_when_already_stopped(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    loop, sleeper = make_loop(kit)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(loop.run(stop), 5)
    assert kit.max.to_users == [] and kit.repo.weekly_reports == {} and sleeper.naps == []


async def test_run_returns_promptly_when_stopped_while_sleeping(tmp_path: Path) -> None:
    """Настоящий сон на минуту: остановка будит его сразу, а не через 60 секунд."""
    kit = await families(tmp_path, SLOT - timedelta(hours=1))
    loop = WeeklyReportLoop(
        ParentReporter(kit.ctx, kit.repo),
        kit.repo,
        kit.ctx.events,
        WeeklySchedule(),
        clock=kit.clock,
    )
    stop = asyncio.Event()

    running = asyncio.create_task(loop.run(stop))
    await settle()
    assert not running.done()
    stop.set()
    await asyncio.wait_for(running, 1)
    assert kit.max.to_users == []


async def test_run_survives_failing_ticks(tmp_path: Path) -> None:
    kit = await families(tmp_path)
    reports = Flaky(kit.repo)
    reports.failures["due_parents"] = 2
    loop, sleeper = make_loop(kit, reports=reports)
    stop = asyncio.Event()

    def stop_when_sent() -> None:
        if len(kit.max.to_users) == len(PARENTS):
            stop.set()

    sleeper.on_nap = stop_when_sent
    await asyncio.wait_for(loop.run(stop), 5)

    assert receivers(kit) == list(PARENTS)
    assert len(kit.events("weekly_loop_failed")) == 2
