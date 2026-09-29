"""Расписание отчёта родителю раз в неделю: слот, догоняющая отправка, тихие часы, период.

Слот по умолчанию — воскресенье 18:00 по Москве. 27.09.2026 и 04.10.2026 — воскресенья.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from hwcheck.bot.report_schedule import (
    WeeklySchedule,
    deliverable,
    last_slot,
    next_slot,
    previous_period,
    weekly_period,
)
from hwcheck.bot.report_text import period_label

MSK = timezone(timedelta(hours=3))
DEFAULT = WeeklySchedule()
WEEK = timedelta(days=7)


def msk(month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=MSK)


SLOT = msk(10, 4, 18)  # воскресенье 04.10.2026, 18:00 мск
PREVIOUS = msk(9, 27, 18)


# --- слот ---


def test_slot_starts_exactly_at_the_hour() -> None:
    assert last_slot(msk(10, 4, 17, 59, 59), DEFAULT) == PREVIOUS
    assert last_slot(msk(10, 4, 18, 0, 0), DEFAULT) == SLOT
    assert last_slot(msk(10, 4, 18, 0, 1), DEFAULT) == SLOT


@pytest.mark.parametrize("day", [28, 29, 30])
def test_every_weekday_of_september_belongs_to_the_last_sunday(day: int) -> None:
    for hour in (0, 12, 23):
        assert last_slot(msk(9, day, hour), DEFAULT) == PREVIOUS


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (msk(9, 27, 18), PREVIOUS),  # воскресенье, сам слот
        (msk(9, 28, 9), PREVIOUS),  # понедельник
        (msk(9, 29, 9), PREVIOUS),  # вторник
        (msk(9, 30, 9), PREVIOUS),  # среда
        (msk(10, 1, 9), PREVIOUS),  # четверг
        (msk(10, 2, 9), PREVIOUS),  # пятница
        (msk(10, 3, 9), PREVIOUS),  # суббота
        (msk(10, 4, 9), PREVIOUS),  # воскресенье до 18:00
        (msk(10, 4, 23), SLOT),
    ],
)
def test_last_slot_on_every_weekday(now: datetime, expected: datetime) -> None:
    assert last_slot(now, DEFAULT) == expected


def test_slot_is_utc_whatever_the_timezone_of_the_clock() -> None:
    slot = last_slot(msk(10, 4, 18), DEFAULT)
    assert slot.tzinfo is UTC and slot == datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
    # те же мгновения, часы в другом поясе: в Новосибирске уже понедельник, в Москве — ещё нет
    novosibirsk = timezone(timedelta(hours=7))
    late = datetime(2026, 10, 5, 1, 0, tzinfo=novosibirsk)  # 04.10 21:00 мск
    assert last_slot(late, DEFAULT) == SLOT
    early = datetime(2026, 10, 4, 14, 59, tzinfo=UTC)  # 04.10 17:59 мск
    assert last_slot(early, DEFAULT) == PREVIOUS


@pytest.mark.parametrize(
    ("schedule", "now", "expected"),
    [
        # среда 09:00
        (WeeklySchedule(weekday=2, hour=9), msk(9, 30, 8, 59), msk(9, 23, 9)),
        (WeeklySchedule(weekday=2, hour=9), msk(9, 30, 9), msk(9, 30, 9)),
        (WeeklySchedule(weekday=2, hour=9), msk(10, 4, 18), msk(9, 30, 9)),
        # понедельник 21:00
        (WeeklySchedule(weekday=0, hour=21), msk(9, 28, 20, 59), msk(9, 21, 21)),
        (WeeklySchedule(weekday=0, hour=21), msk(9, 28, 21), msk(9, 28, 21)),
        # суббота 12:00
        (WeeklySchedule(weekday=5, hour=12), msk(10, 4, 18), msk(10, 3, 12)),
    ],
)
def test_last_slot_follows_the_schedule(
    schedule: WeeklySchedule, now: datetime, expected: datetime
) -> None:
    assert last_slot(now, schedule) == expected


def test_slot_crosses_the_year() -> None:
    # 03.01.2027 — воскресенье
    now = datetime(2027, 1, 2, 12, 0, tzinfo=MSK)
    assert last_slot(now, DEFAULT) == datetime(2026, 12, 27, 18, 0, tzinfo=MSK)
    assert next_slot(now, DEFAULT) == datetime(2027, 1, 3, 18, 0, tzinfo=MSK)


def test_next_slot_is_a_week_after_the_last_one() -> None:
    assert next_slot(msk(10, 4, 17, 59, 59), DEFAULT) == SLOT
    assert next_slot(msk(10, 4, 18), DEFAULT) == SLOT + WEEK
    assert next_slot(msk(9, 30, 12), DEFAULT) == SLOT
    assert next_slot(msk(9, 30, 12), DEFAULT).tzinfo is UTC
    assert next_slot(msk(9, 30, 12), WeeklySchedule(weekday=2, hour=9)) == msk(10, 7, 9)


def test_naive_time_is_refused() -> None:
    """Время без пояса сдвинуло бы слот на три часа молча: лучше ошибка."""
    naive = datetime(2026, 10, 4, 18, 0)
    for function in (last_slot, next_slot):
        with pytest.raises(ValueError, match="пояс"):
            function(naive, DEFAULT)
    with pytest.raises(ValueError, match="пояс"):
        deliverable(naive, SLOT, DEFAULT)
    with pytest.raises(ValueError, match="пояс"):
        deliverable(SLOT, naive, DEFAULT)
    for period in (weekly_period, previous_period):
        with pytest.raises(ValueError, match="пояс"):
            period(naive)


# --- догоняющая отправка и тихие часы ---


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (msk(10, 4, 17, 59, 59), False),  # слот ещё не наступил
        (msk(10, 4, 18), True),
        (msk(10, 4, 21, 59, 59), True),
        (msk(10, 4, 22), False),  # тихие часы
        (msk(10, 4, 22, 30), False),
        (msk(10, 5, 3), False),
        (msk(10, 5, 8, 59), False),
        (msk(10, 5, 8, 59, 59), False),
        (msk(10, 5, 9), True),  # бот лежал в воскресенье — догоняем в понедельник
        (msk(10, 5, 20, 59), True),
        (msk(10, 5, 20, 59, 59), True),
        (msk(10, 5, 21), False),  # слот + 27 часов: неделя пропущена
        (msk(10, 6, 12), False),
        (msk(10, 10, 12), False),
    ],
)
def test_report_is_delivered_in_the_window_and_not_at_night(now: datetime, expected: bool) -> None:
    assert deliverable(now, SLOT, DEFAULT) is expected


def test_window_belongs_to_its_slot() -> None:
    """Воскресенье 18:00 — уже окно нового слота; прошлый слот в это время не догоняется."""
    assert deliverable(SLOT, SLOT, DEFAULT)
    assert not deliverable(SLOT, PREVIOUS, DEFAULT)
    assert not deliverable(PREVIOUS - timedelta(seconds=1), PREVIOUS, DEFAULT)


def test_window_and_quiet_hours_follow_the_schedule() -> None:
    short = WeeklySchedule(catchup=timedelta(hours=2))
    assert deliverable(msk(10, 4, 19, 59), SLOT, short)
    assert not deliverable(msk(10, 4, 20), SLOT, short)
    # тихие часы внутри одних суток: с 13 до 15
    siesta = WeeklySchedule(weekday=6, hour=12, quiet=(13, 15))
    noon = msk(10, 4, 12)
    assert deliverable(msk(10, 4, 12, 59), noon, siesta)
    assert not deliverable(msk(10, 4, 13), noon, siesta)
    assert not deliverable(msk(10, 4, 14, 59), noon, siesta)
    assert deliverable(msk(10, 4, 15), noon, siesta)
    assert deliverable(msk(10, 5, 3), noon, siesta)  # ночью этому расписанию можно


def test_quiet_hours_are_counted_by_moscow_time() -> None:
    assert not deliverable(datetime(2026, 10, 4, 19, 0, tzinfo=UTC), SLOT, DEFAULT)  # 22:00 мск
    assert deliverable(datetime(2026, 10, 5, 6, 0, tzinfo=UTC), SLOT, DEFAULT)  # 09:00 мск


# --- расписание ---


@pytest.mark.parametrize("hour", [22, 23, 0, 3, 8])
def test_hour_in_quiet_hours_is_refused(hour: int) -> None:
    with pytest.raises(ValueError, match="тихие часы"):
        WeeklySchedule(hour=hour)


@pytest.mark.parametrize("hour", [9, 12, 18, 21])
def test_hour_outside_quiet_hours_is_accepted(hour: int) -> None:
    assert WeeklySchedule(hour=hour).hour == hour


@pytest.mark.parametrize(
    "values",
    [
        {"weekday": -1},
        {"weekday": 7},
        {"hour": 24},
        {"hour": -1},
        {"catchup": timedelta(0)},
        {"catchup": timedelta(hours=-1)},
        {"quiet": (22, 24)},
        {"quiet": (-1, 9)},
    ],
)
def test_impossible_schedule_is_refused(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WeeklySchedule(**values)  # type: ignore[arg-type]


def test_default_schedule_is_sunday_evening() -> None:
    assert (DEFAULT.weekday, DEFAULT.hour) == (6, 18)
    assert DEFAULT.catchup == timedelta(hours=27) and DEFAULT.quiet == (22, 9)


# --- период ---


def test_weekly_period_is_seven_days_before_the_slot() -> None:
    period = weekly_period(SLOT)
    assert (period.start, period.end) == (PREVIOUS, SLOT)
    assert period.start.tzinfo is UTC and period.end.tzinfo is UTC
    assert (period.first_day, period.last_day) == (date(2026, 9, 27), date(2026, 10, 4))
    assert period_label(period.first_day, period.last_day) == "27 сентября – 4 октября"


def test_previous_period_is_the_week_before() -> None:
    period = previous_period(SLOT)
    assert (period.start, period.end) == (PREVIOUS - WEEK, PREVIOUS)
    assert (period.first_day, period.last_day) == (date(2026, 9, 20), date(2026, 9, 27))
    assert period_label(period.first_day, period.last_day) == "20–27 сентября"
    assert previous_period(SLOT) == weekly_period(PREVIOUS)


def test_weeks_follow_each_other_without_gaps_and_overlaps() -> None:
    """Каждая домашка попадает ровно в один отчёт: конец недели — начало следующей."""
    first, second = weekly_period(SLOT), weekly_period(SLOT + WEEK)
    assert first.end == second.start
    assert previous_period(SLOT).end == first.start


def test_period_across_the_year() -> None:
    slot = datetime(2027, 1, 3, 18, 0, tzinfo=MSK)
    period = weekly_period(slot)
    assert (period.first_day, period.last_day) == (date(2026, 12, 27), date(2027, 1, 3))
    assert period_label(period.first_day, period.last_day) == "27 декабря 2026 – 3 января 2027"
    before = previous_period(slot)
    assert period_label(before.first_day, before.last_day) == "20–27 декабря"


def test_period_days_are_moscow_days() -> None:
    """Слот в 09:00 мск — это 06:00 UTC того же дня; в 00 часов UTC дата была бы вчерашней."""
    slot = last_slot(msk(9, 30, 12), WeeklySchedule(weekday=2, hour=9))
    period = weekly_period(slot)
    assert (period.first_day, period.last_day) == (date(2026, 9, 23), date(2026, 9, 30))
    assert period_label(period.first_day, period.last_day) == "23–30 сентября"
