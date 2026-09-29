"""Раннер и отчёт раз в неделю: настройки, когда цикл рассылки запускается и как он
останавливается вместе с ботом."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import settle
from hwcheck.bot import runner
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.bot.report_schedule import WeeklyReportLoop, WeeklySchedule
from hwcheck.bot.runner import (
    _poll_with_weekly,
    log_weekly_mode,
    make_weekly_loop,
    weekly_schedule,
)
from hwcheck.config import Settings
from onboarding_kit import make_kit

WEDNESDAY = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)  # 12:00 мск
SUNDAY_EVENING = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)  # 19:00 мск, час после слота


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


# --- настройки ---


def test_weekly_report_is_off_until_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    default = settings()
    assert default.weekly_report is False  # аварийный выключатель: рассылку включают явно
    assert (default.weekly_report_weekday, default.weekly_report_hour) == (6, 18)
    assert default.weekly_report_pause_s == 0.5
    assert weekly_schedule(default) == WeeklySchedule()

    monkeypatch.setenv("WEEKLY_REPORT", "true")
    monkeypatch.setenv("WEEKLY_REPORT_WEEKDAY", "2")
    monkeypatch.setenv("WEEKLY_REPORT_HOUR", "9")
    monkeypatch.setenv("WEEKLY_REPORT_PAUSE_S", "1.5")
    chosen = settings()
    assert chosen.weekly_report is True and chosen.weekly_report_pause_s == 1.5
    assert weekly_schedule(chosen) == WeeklySchedule(weekday=2, hour=9)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEEKLY_REPORT_WEEKDAY", "-1"),
        ("WEEKLY_REPORT_WEEKDAY", "7"),
        ("WEEKLY_REPORT_HOUR", "-1"),
        ("WEEKLY_REPORT_HOUR", "24"),
        ("WEEKLY_REPORT_PAUSE_S", "-0.5"),
        ("WEEKLY_REPORT_HOUR", "18:00"),
        ("WEEKLY_REPORT", "sunday"),
    ],
)
def test_bad_weekly_setting_stops_the_bot_at_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name.lower()):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["0", "6"])
def test_weekday_bounds_are_monday_and_sunday(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WEEKLY_REPORT_WEEKDAY", value)
    assert weekly_schedule(Settings(_env_file=None)).weekday == int(value)


@pytest.mark.parametrize("hour", [22, 23, 0, 5, 8])
def test_hour_in_quiet_hours_stops_the_bot_at_start(hour: int) -> None:
    """В тихие часы бот родителям не пишет: отчёт с таким часом уходил бы только утром."""
    with pytest.raises(SystemExit, match=f"WEEKLY_REPORT_HOUR={hour}: .*с 9 до 21"):
        weekly_schedule(settings(weekly_report_hour=hour))
    with pytest.raises(SystemExit, match="WEEKLY_REPORT_HOUR"):  # и при выключенной рассылке
        weekly_schedule(settings(weekly_report=False, weekly_report_hour=hour))


@pytest.mark.parametrize("hour", [9, 21])
def test_hours_next_to_quiet_hours_are_accepted(hour: int) -> None:
    assert weekly_schedule(settings(weekly_report_hour=hour)).hour == hour


async def test_bad_hour_stops_the_bot_before_anything_is_opened(tmp_path: Path) -> None:
    broken = settings(
        max_token="token", events_path=str(tmp_path / "events.jsonl"), weekly_report_hour=23
    )
    with pytest.raises(SystemExit, match="WEEKLY_REPORT_HOUR=23"):
        await runner.run_polling(broken)


# --- когда цикл запускается ---


def make_onboarding(tmp_path: Path, **options: Any) -> Onboarding:
    kit = make_kit(tmp_path)
    return Onboarding(kit.ctx, kit.repo, **options)


def test_loop_needs_the_setting_and_onboarding(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    onboarding = Onboarding(kit.ctx, kit.repo)
    pool: Any = object()  # вместо пула: хранилище отчёта его только запоминает
    events = kit.ctx.events
    on, off = settings(weekly_report=True), settings()

    assert make_weekly_loop(off, onboarding, pool, events) is None
    assert make_weekly_loop(on, None, pool, events) is None  # онбординг выключен
    assert make_weekly_loop(on, None, None, events) is None  # бот без базы
    assert make_weekly_loop(on, Onboarding(kit.ctx), pool, events) is None  # отчёта нет
    assert isinstance(make_weekly_loop(on, onboarding, pool, events), WeeklyReportLoop)


def test_loop_takes_schedule_and_pause_from_settings(tmp_path: Path) -> None:
    chosen = settings(
        weekly_report=True,
        weekly_report_weekday=2,
        weekly_report_hour=9,
        weekly_report_pause_s=1.5,
    )
    schedule = weekly_schedule(chosen)
    onboarding = make_onboarding(tmp_path, schedule=schedule)
    kit = make_kit(tmp_path)
    pool: Any = object()

    loop = make_weekly_loop(chosen, onboarding, pool, kit.ctx.events)

    assert loop is not None
    assert loop._schedule == WeeklySchedule(weekday=2, hour=9)
    assert (loop._pause_s, loop._tick_s, loop._send_timeout_s) == (1.5, 60.0, 20.0)
    assert loop._reporter is onboarding.reporter  # кнопки под отчётом и в ответ — одни и те же
    assert loop._reports._pool is pool


def test_start_line_says_whether_weekly_report_is_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    onboarding = make_onboarding(tmp_path)
    kit = make_kit(tmp_path)
    pool: Any = object()
    on, off = settings(weekly_report=True), settings()
    loop = make_weekly_loop(on, onboarding, pool, kit.ctx.events)

    with caplog.at_level(logging.INFO, logger="hwcheck.bot.runner"):
        log_weekly_mode(off, None, WEDNESDAY)
        log_weekly_mode(on, loop, WEDNESDAY)
        log_weekly_mode(on, loop, SUNDAY_EVENING)
        log_weekly_mode(on, None, WEDNESDAY)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "weekly report: off"),
        (logging.INFO, "weekly report: on, next slot 2026-10-04 18:00 MSK"),
        (
            logging.INFO,
            "weekly report: on, next slot 2026-10-11 18:00 MSK, catching up 2026-10-04 18:00 MSK",
        ),
        (
            logging.WARNING,
            "weekly report: off — WEEKLY_REPORT=true, но онбординг выключен или нет базы",
        ),
    ]


def test_start_line_follows_the_schedule(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    chosen = settings(weekly_report=True, weekly_report_weekday=2, weekly_report_hour=9)
    onboarding = make_onboarding(tmp_path, schedule=weekly_schedule(chosen))
    pool: Any = object()
    loop = make_weekly_loop(chosen, onboarding, pool, make_kit(tmp_path).ctx.events)

    with caplog.at_level(logging.INFO, logger="hwcheck.bot.runner"):
        log_weekly_mode(chosen, loop, WEDNESDAY + timedelta(days=1))
    assert [r.getMessage() for r in caplog.records] == [
        "weekly report: on, next slot 2026-10-07 09:00 MSK"
    ]


# --- цикл рядом с опросом MAX ---


class FakeWeekly:
    """Вместо цикла рассылки: помнит, запускали ли его и чем он кончился."""

    def __init__(self, *, finishing_s: float = 0.0, obeys_stop: bool = True) -> None:
        self.started = asyncio.Event()
        self.finished = False
        self.cancelled = False
        self._finishing_s = finishing_s
        self._obeys_stop = obeys_stop

    async def run(self, stop: asyncio.Event) -> None:
        self.started.set()
        try:
            await (stop.wait() if self._obeys_stop else asyncio.Event().wait())
            await asyncio.sleep(self._finishing_s)  # начатая отправка доходит до конца
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True


async def polling_until(stop: asyncio.Event, weekly: FakeWeekly) -> None:
    """Вместо опроса MAX: работает, пока не придёт остановка."""
    await asyncio.wait_for(weekly.started.wait(), 5)
    stop.set()


async def test_polling_runs_alone_without_weekly_report() -> None:
    done: list[str] = []

    async def polling() -> None:
        done.append("polled")

    await asyncio.wait_for(_poll_with_weekly(None, asyncio.Event(), polling()), 5)
    assert done == ["polled"]


async def test_loop_runs_next_to_polling_and_finishes_on_stop() -> None:
    weekly, stop = FakeWeekly(finishing_s=0.01), asyncio.Event()
    work: Any = weekly

    await asyncio.wait_for(_poll_with_weekly(work, stop, polling_until(stop, weekly)), 5)

    # начатый отчёт доведён до конца: отменять было нечего
    assert weekly.finished and not weekly.cancelled


async def test_loop_that_ignores_stop_is_cancelled_after_a_short_wait(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runner, "WEEKLY_STOP_TIMEOUT_S", 0.01)
    weekly, stop = FakeWeekly(obeys_stop=False), asyncio.Event()
    work: Any = weekly

    with caplog.at_level(logging.WARNING, logger="hwcheck.bot.runner"):
        await asyncio.wait_for(_poll_with_weekly(work, stop, polling_until(stop, weekly)), 5)

    assert weekly.cancelled and not weekly.finished
    assert [r.getMessage() for r in caplog.records] == [
        "weekly report loop cancelled: not finished 0.01 s after stop"
    ]


async def test_loop_stuck_after_cancel_does_not_block_shutdown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runner, "WEEKLY_STOP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(runner, "CANCEL_GRACE_S", 0.01)
    release = asyncio.Event()

    class Stubborn(FakeWeekly):
        async def run(self, stop: asyncio.Event) -> None:
            self.started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled = True  # отмену проглотил

    weekly, stop = Stubborn(), asyncio.Event()
    work: Any = weekly
    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.runner"):
        await asyncio.wait_for(_poll_with_weekly(work, stop, polling_until(stop, weekly)), 5)

    assert weekly.cancelled
    assert [r.getMessage() for r in caplog.records] == [
        "weekly report loop still running 0.01 s after cancel"
    ]
    release.set()
    await settle()


async def test_failed_polling_cancels_the_loop_at_once() -> None:
    weekly = FakeWeekly()
    work: Any = weekly

    async def broken() -> None:
        await weekly.started.wait()
        raise RuntimeError("polling crashed")

    with pytest.raises(RuntimeError, match="polling crashed"):
        await asyncio.wait_for(_poll_with_weekly(work, asyncio.Event(), broken()), 5)
    assert weekly.cancelled  # остановки не было: ждать рассылке нечего


async def test_cancelled_runner_does_not_leave_the_loop_running() -> None:
    """Ctrl+C: раннер отменён — рассылка отменяется вместе с ним, а не живёт после закрытия
    клиентов MAX и базы."""
    weekly = FakeWeekly()
    work: Any = weekly
    idle = asyncio.Event().wait()

    running = asyncio.create_task(_poll_with_weekly(work, asyncio.Event(), idle))
    await asyncio.wait_for(weekly.started.wait(), 5)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert weekly.cancelled


async def test_crashed_loop_is_logged_and_does_not_stop_the_bot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Crashing(FakeWeekly):
        async def run(self, stop: asyncio.Event) -> None:
            self.started.set()
            raise ZeroDivisionError("bug in the loop")

    weekly, stop = Crashing(), asyncio.Event()
    work: Any = weekly
    polled: list[str] = []

    async def polling() -> None:
        await weekly.started.wait()
        await settle()
        polled.append("still polling")
        stop.set()

    with caplog.at_level(logging.ERROR, logger="hwcheck.bot.runner"):
        await asyncio.wait_for(_poll_with_weekly(work, stop, polling()), 5)

    assert polled == ["still polling"]
    assert [r.getMessage() for r in caplog.records] == [
        "weekly report loop crashed: ZeroDivisionError"
    ]


# --- run_polling ---


class FakeResource:
    def __init__(self, *_args: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeResource":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def me(self) -> dict[str, Any]:
        return {"name": "bot"}


async def run_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, weekly: FakeWeekly | None, **values: Any
) -> list[Any]:
    """Бот с подставными MAX и моделями; опрос MAX идёт, пока рассылка не стартует (или один
    оборот, если её нет), затем — остановка."""
    seen: list[Any] = []

    async def polling(*args: Any) -> None:
        stop: asyncio.Event = args[3]
        await settle()
        seen.append(weekly.started.is_set() if weekly is not None else None)
        stop.set()

    def fake_loop(*args: Any) -> FakeWeekly | None:
        seen.append(args)
        return weekly

    monkeypatch.setattr(runner, "MaxClient", FakeResource)
    monkeypatch.setattr(runner, "make_llm", FakeResource)
    monkeypatch.setattr(runner, "_poll_loop", polling)
    monkeypatch.setattr(runner, "_install_stop_handler", lambda *_args: None)
    monkeypatch.setattr(runner, "make_weekly_loop", fake_loop)
    configured = settings(
        max_token="token",
        events_path=str(tmp_path / "events.jsonl"),
        photos_ttl_days=0,
        kb_photos_ttl_days=0,
        **values,
    )
    await asyncio.wait_for(runner.run_polling(configured), 5)
    return seen


async def test_bot_runs_the_loop_and_finishes_it_on_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    weekly = FakeWeekly()
    with caplog.at_level(logging.INFO, logger="hwcheck.bot.runner"):
        args, started = await run_bot(monkeypatch, tmp_path, weekly, weekly_report=True)

    assert started is True and weekly.finished and not weekly.cancelled
    configured, onboarding, pool, _events = args
    assert configured.weekly_report is True
    assert onboarding is None and pool is None  # бот без базы: решает `make_weekly_loop`
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if m.startswith("weekly report: on, next slot ")]) == 1
    assert messages[-1] == "bot stopped"


async def test_bot_without_the_loop_only_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="hwcheck.bot.runner"):
        _args, started = await run_bot(monkeypatch, tmp_path, None)

    assert started is None
    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if m.startswith("weekly report")] == ["weekly report: off"]


async def test_real_loop_is_not_started_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Настройка выключена — цикла нет совсем: ни задачи, ни запросов к базе."""
    started: list[str] = []

    async def polling(*args: Any) -> None:
        await settle()

    async def never(self: Any, stop: asyncio.Event) -> None:
        started.append("weekly")

    monkeypatch.setattr(runner, "MaxClient", FakeResource)
    monkeypatch.setattr(runner, "make_llm", FakeResource)
    monkeypatch.setattr(runner, "_poll_loop", polling)
    monkeypatch.setattr(runner, "_install_stop_handler", lambda *_args: None)
    monkeypatch.setattr(WeeklyReportLoop, "run", never)
    configured = settings(
        max_token="token",
        events_path=str(tmp_path / "events.jsonl"),
        photos_ttl_days=0,
        kb_photos_ttl_days=0,
    )
    await asyncio.wait_for(runner.run_polling(configured), 5)
    assert started == []
