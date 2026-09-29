"""Отчёт родителю раз в неделю: что уходит родителю, что — в журнал, и выключатель под отчётом.

Слот — воскресенье 20.09.2026 18:00 мск: неделя отчёта — с 13.09 18:00 по 20.09 18:00. Часы
набора — 16.09.2026 12:00 UTC: домашка, записанная «сейчас», попадает в эту неделю.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.max_api import Buttons
from hwcheck.bot.report import ParentReporter
from hwcheck.bot.report_schedule import WeeklySchedule
from hwcheck.db.repo import Account
from hwcheck.events import EventLog
from onboarding_kit import Kit, actor, chat, payloads, ready_student
from test_parent_report import (
    CHILD,
    FIRST_ID,
    PARENT,
    YOUNG_PARENT,
    BrokenReports,
    check,
    make_family_kit,
    parent_of,
    young_child,
)

SLOT = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
WEEK = timedelta(days=7)
HEADER = "📈 Отчёт за неделю: 14–20 сентября"
EMPTY_WEEK = f"{HEADER}\n\nНа этой неделе домашку на проверку не присылали."
SWITCH_OFF: Buttons = [
    [{"type": "callback", "text": "Не присылать по воскресеньям", "payload": "ob:weekly:off"}]
]
SWITCH_ON: Buttons = [
    [{"type": "callback", "text": "Присылать по воскресеньям", "payload": "ob:weekly:on"}]
]
WEEKLY_FIELDS = {"kind": "weekly", "component": "report", "user_initiated": False}
REPORT_EVENTS = ("parent_report_sent", "parent_report_failed", "parent_report_skipped")


async def send(
    kit: Kit, user_id: int = PARENT, *, slot: datetime = SLOT, reports: Any = None
) -> str:
    reporter = ParentReporter(kit.ctx, reports if reports is not None else kit.repo)
    return await reporter.send_weekly(await parent_of(kit, user_id), slot)


def report_events(kit: Kit) -> list[dict[str, Any]]:
    return [row for row in kit.events() if row["type"] in REPORT_EVENTS]


async def test_parent_gets_weekly_report_about_his_children(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    older = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    younger = await young_child(kit, 3, PARENT)
    await check(kit, older.id, 12, 9, 2, 1, resolved=1)
    await check(kit, older.id, 11, 9, 1, 1, resolved=1)
    await check(kit, younger, 9, 9, 0, 0)

    assert await send(kit) == "sent"

    text = (
        f"{HEADER}\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 2 домашки, 23 задания:\n"
        "• верно — 18\n"
        "• с ошибкой — 3, из них разобрал с подсказками — 2\n"
        "• стоит перепроверить — 2\n"
        "\n"
        "Ребёнок (3 класс)\n"
        "Математика — 1 домашка, 9 заданий: все верно ✅"
    )
    # нажатия не было, чата для ответа нет: отчёт уходит родителю по его id MAX
    assert kit.max.to_users == [(PARENT, text, SWITCH_OFF)]
    assert kit.max.sent == []
    [sent] = report_events(kit)
    assert sent["type"] == "parent_report_sent"
    assert {key: sent[key] for key in WEEKLY_FIELDS} == WEEKLY_FIELDS
    assert (sent["children"], sent["homeworks"], sent["empty"]) == (2, 3, False)
    assert sent["user"] == actor(PARENT).user_hash


async def test_under_weekly_report_there_is_only_the_switch(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)

    await send(kit)

    [(_, _, buttons)] = kit.max.to_users
    assert payloads(buttons) == ["ob:weekly:off"]  # кнопки отчёта по запросу здесь нет
    assert ":" not in payloads(buttons)[0].removeprefix("ob:weekly:")  # и ничьих id


async def test_week_is_seven_days_before_the_slot(tmp_path: Path) -> None:
    """Домашка попадает ровно в один отчёт: начало недели входит, час рассылки — уже нет."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    moment = timedelta(microseconds=1)
    for when, tasks in (
        (SLOT - WEEK - moment, 1),  # прошлая неделя
        (SLOT - WEEK, 2),
        (SLOT - moment, 4),
        (SLOT, 8),  # следующая неделя
    ):
        kit.clock.now = when
        await check(kit, child.id, tasks, tasks, 0, 0)

    assert await send(kit) == "sent"
    [(_, text, _)] = kit.max.to_users
    assert text == (
        f"{HEADER}\n\nРебёнок (7 класс)\nМатематика — 2 домашки, 6 заданий: все верно ✅"
    )
    assert await send(kit, slot=SLOT + WEEK) == "sent"
    assert kit.max.to_users[-1][1] == (
        "📈 Отчёт за неделю: 21–27 сентября\n\n"
        "Ребёнок (7 класс)\nМатематика — 1 домашка, 8 заданий: все верно ✅"
    )


async def test_child_without_homework_is_shown_next_to_child_with_homework(
    tmp_path: Path,
) -> None:
    kit = make_family_kit(tmp_path)
    first = await young_child(kit, 2)
    await young_child(kit, 4)
    await check(kit, first, 3, 3, 0, 0)

    assert await send(kit, YOUNG_PARENT) == "sent"
    assert kit.max.to_users[-1][1] == (
        f"{HEADER}\n"
        "\n"
        "Ребёнок (2 класс)\n"
        "Математика — 1 домашка, 3 задания: все верно ✅\n"
        "\n"
        "Ребёнок (4 класс)\n"
        "Проверок за эти дни не было."
    )


async def test_first_empty_week_is_reported_shortly(tmp_path: Path) -> None:
    """Неделей раньше домашки были, на этой — нет: родитель узнаёт об этом один раз."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    kit.clock.now = SLOT - WEEK - timedelta(hours=1)
    await check(kit, child.id, 4, 2, 2, 0)

    assert await send(kit) == "sent"

    assert kit.max.to_users == [(PARENT, EMPTY_WEEK, SWITCH_OFF)]
    [sent] = report_events(kit)
    assert sent["type"] == "parent_report_sent"
    assert (sent["children"], sent["homeworks"], sent["empty"]) == (1, 0, True)
    assert {key: sent[key] for key in WEEKLY_FIELDS} == WEEKLY_FIELDS


async def test_second_empty_week_is_skipped(tmp_path: Path) -> None:
    """Семья не пользуется ботом: «пустой» отчёт каждую неделю был бы спамом."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    kit.clock.now = SLOT - 2 * WEEK - timedelta(hours=1)  # две недели назад
    await check(kit, child.id, 4, 2, 2, 0)

    assert await send(kit) == "skipped"

    assert kit.max.to_users == [] and kit.max.sent == []
    [skipped] = report_events(kit)
    assert (skipped["type"], skipped["reason"]) == ("parent_report_skipped", "empty")
    assert {key: skipped[key] for key in WEEKLY_FIELDS} == WEEKLY_FIELDS
    assert skipped["user"] == actor(PARENT).user_hash
    assert kit.events("notify_sent") == []


async def test_family_that_never_sent_homework_gets_nothing(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await young_child(kit, 3)
    assert await send(kit, YOUNG_PARENT) == "skipped"
    assert kit.max.to_users == []
    assert [row["reason"] for row in report_events(kit)] == ["empty"]


async def test_parent_whose_children_lost_consent_gets_nothing(tmp_path: Path) -> None:
    """Согласие отозвано между выбором получателей и отправкой: данные ребёнка не уходят."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 4, 2, 2, 0)
    [consent] = kit.repo.consents
    consent.revoked_at = kit.clock.now

    assert await send(kit) == "skipped"

    assert kit.max.to_users == []
    [skipped] = report_events(kit)
    assert (skipped["type"], skipped["reason"]) == ("parent_report_skipped", "no_children")


async def test_homework_of_child_without_consent_does_not_count(tmp_path: Path) -> None:
    """Проверки ребёнка с отозванным согласием — не повод ни для отчёта, ни для «пустого»."""
    kit = make_family_kit(tmp_path)
    older = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await young_child(kit, 3, PARENT)
    await check(kit, older.id, 4, 2, 2, 0)
    kit.clock.now = SLOT - WEEK - timedelta(hours=1)
    await check(kit, older.id, 4, 2, 2, 0)
    kit.repo.consents[0].revoked_at = kit.clock.now

    assert await send(kit) == "skipped"
    assert kit.max.to_users == []
    assert [row["reason"] for row in report_events(kit)] == ["empty"]


async def test_blocked_parent_is_a_failed_report_not_an_exception(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)
    kit.max.blocked_users.add(PARENT)

    assert await send(kit) == "failed"

    assert kit.max.to_users == []
    [failed] = report_events(kit)
    assert failed["type"] == "parent_report_failed"
    assert {key: failed[key] for key in WEEKLY_FIELDS} == WEEKLY_FIELDS
    assert failed["error"] == "RuntimeError"
    assert failed["user"] == actor(PARENT).user_hash


async def test_storage_failure_is_a_failed_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    assert await send(kit, reports=BrokenReports()) == "failed"

    assert kit.max.to_users == []
    [failed] = report_events(kit)
    assert (failed["type"], failed["error"]) == ("parent_report_failed", "ConnectionError")
    # текст исключения — с id родителя из базы: ни в журнал, ни в лог он не идёт
    assert "database is down" not in json.dumps(kit.events(), ensure_ascii=False)
    assert "database is down" not in caplog.text


async def test_undecryptable_id_is_a_failed_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ключ шифра сменили: написать родителю нельзя, узнать его для журнала — тоже. Сбой
    остаётся в логе, а рассылка идёт дальше."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)
    parent = replace(await parent_of(kit), user_id_enc=b"not-a-token")

    with caplog.at_level(logging.WARNING, logger="hwcheck.bot.report"):
        status = await ParentReporter(kit.ctx, kit.repo).send_weekly(parent, SLOT)

    assert status == "failed"
    assert kit.max.to_users == [] and kit.events() == []
    assert [record.getMessage() for record in caplog.records] == [
        "weekly report failed: UserIdCipherError",
        "parent_report_failed not written: UserIdCipherError",
    ]


async def test_journal_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт родителю уже ушёл: отказ журнала (диск) не делает его неотправленным."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)

    def full_disk(*_args: Any, **_kw: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(kit.ctx.events, "log", full_disk)
    assert await send(kit) == "sent"
    assert len(kit.max.to_users) == 1


async def test_journal_has_no_max_id_and_no_profile_id(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    older = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    younger = await young_child(kit, 3, PARENT)
    await check(kit, older.id, 4, 3, 1, 0)
    parent = await parent_of(kit)

    assert await send(kit) == "sent"
    assert await send(kit, reports=BrokenReports()) == "failed"
    assert await send(kit, slot=SLOT + 3 * WEEK) == "skipped"
    kit.max.blocked_users.add(PARENT)
    assert await send(kit) == "failed"

    rows = report_events(kit)
    assert [row["type"].removeprefix("parent_report_") for row in rows] == [
        "sent", "failed", "skipped", "failed"
    ]  # fmt: skip
    fields = {"ts", "env", "trace_id", "type", "user", "component", "user_initiated", "kind"}
    assert set(rows[0]) == fields | {"children", "homeworks", "empty"}
    assert set(rows[1]) == set(rows[3]) == fields | {"error"}
    assert set(rows[2]) == fields | {"reason"}
    assert min(older.id, younger, parent.id) > FIRST_ID  # id из базы — приметные
    assert kit.events() == rows  # других событий рассылка не пишет
    for row in rows:
        assert row["user"] == actor(PARENT).user_hash and row["user_initiated"] is False
        # время записи — не данные семьи, а в его цифрах id мог бы совпасть случайно
        written = json.dumps({k: v for k, v in row.items() if k != "ts"}, ensure_ascii=False)
        for secret in (PARENT, CHILD, chat(PARENT), older.id, younger, parent.id):
            assert str(secret) not in written


async def test_events_of_tester_stay_out_of_the_contest(tmp_path: Path) -> None:
    """Тестера журнал узнаёт сам (`TEST_USERS`) — и в событиях рассылки, где нажатия нет."""
    kit = make_family_kit(tmp_path)
    tester, regular = actor(PARENT).user_hash, actor(YOUNG_PARENT).user_hash
    log = EventLog(kit.events_path, "prod", test_users={tester})
    kit = replace(kit, ctx=replace(kit.ctx, events=log))
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)
    await check(kit, await young_child(kit, 3), 2, 2, 0, 0)

    assert await send(kit) == "sent"
    assert await send(kit, YOUNG_PARENT) == "sent"
    assert await send(kit, slot=SLOT + 3 * WEEK) == "skipped"

    rows = kit.events()
    assert {row["type"] for row in rows} >= {"parent_report_sent", "parent_report_skipped"}
    assert {row["env"] for row in rows if row["user"] == tester} == {"test"}
    assert {row["env"] for row in rows if row["user"] == regular} == {"prod"}
    assert {row["user"] for row in rows} == {tester, regular}


# --- выключатель ---


async def switch(kit: Kit, enabled: bool, schedule: WeeklySchedule | None = None) -> Account:
    parent = await parent_of(kit)
    reporter = ParentReporter(kit.ctx, kit.repo, schedule)
    await reporter.switch_weekly(actor(PARENT), parent, enabled=enabled)
    return parent


async def test_parent_switches_weekly_report_off_and_back(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    parent = await switch(kit, False)
    assert not await kit.repo.weekly_enabled(parent.id)
    assert kit.last(PARENT) == (
        "Хорошо, отчёт по воскресеньям больше не присылаю. Отчёт по кнопке «📈 Отчёт о "
        "прогрессе» остаётся. Вернуть рассылку можно кнопкой ниже.",
        SWITCH_ON,
    )
    await switch(kit, True)
    assert await kit.repo.weekly_enabled(parent.id)
    assert kit.last(PARENT) == (
        "Готово! Снова буду присылать отчёт о прогрессе по воскресеньям.",
        SWITCH_OFF,
    )
    assert kit.max.to_users == []  # ответ — в чат, где нажата кнопка

    rows = kit.events("weekly_report_set")
    assert [row["enabled"] for row in rows] == [False, True]
    for row in rows:
        assert (row["component"], row["user_initiated"]) == ("report", True)
        assert row["user"] == actor(PARENT).user_hash


async def test_weekly_switch_is_independent_of_notifications(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)
    parent = await parent_of(kit)
    await kit.repo.set_notify_mode(parent.id, "off")

    await switch(kit, False)
    assert await kit.repo.notify_mode(parent.id) == "off"
    await kit.repo.set_notify_mode(parent.id, "instant")
    assert not await kit.repo.weekly_enabled(parent.id)


@pytest.mark.parametrize(
    ("weekday", "days"),
    [
        (0, "по понедельникам"),
        (1, "по вторникам"),
        (2, "по средам"),
        (3, "по четвергам"),
        (4, "по пятницам"),
        (5, "по субботам"),
        (6, "по воскресеньям"),
    ],
)
async def test_switch_names_the_day_of_the_schedule(
    tmp_path: Path, weekday: int, days: str
) -> None:
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 3, 3, 0, 0)
    schedule = WeeklySchedule(weekday=weekday, hour=12)

    await switch(kit, False, schedule)
    text, buttons = kit.last(PARENT)
    assert text.startswith(f"Хорошо, отчёт {days} больше не присылаю.")
    assert buttons == [
        [{"type": "callback", "text": f"Присылать {days}", "payload": "ob:weekly:on"}]
    ]
    await switch(kit, True, schedule)
    text, buttons = kit.last(PARENT)
    assert text == f"Готово! Снова буду присылать отчёт о прогрессе {days}."
    assert buttons == [
        [{"type": "callback", "text": f"Не присылать {days}", "payload": "ob:weekly:off"}]
    ]

    reporter = ParentReporter(kit.ctx, kit.repo, schedule)
    assert await reporter.send_weekly(await parent_of(kit), SLOT) == "sent"
    assert kit.max.to_users[-1][2] == buttons  # под отчётом — та же кнопка


async def test_failed_switch_is_seen_by_parent(tmp_path: Path) -> None:
    """Выключатель — действие самого родителя: его сбой не прячется, как и у итогов."""

    class ReadOnly(BrokenReports):
        async def set_weekly(self, parent_user_id: int, enabled: bool) -> None:
            raise ConnectionError("database is down")

    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)
    reporter = ParentReporter(kit.ctx, ReadOnly())
    with pytest.raises(ConnectionError):
        await reporter.switch_weekly(actor(PARENT), await parent_of(kit), enabled=False)
    assert kit.events("weekly_report_set") == [] and kit.max.sent == []
