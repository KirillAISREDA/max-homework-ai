"""Отчёт родителю по запросу (кнопка «📈 Отчёт о прогрессе»): что уходит родителю и что — в журнал.

Часы набора — 16.09.2026 12:00 UTC: период отчёта — 10–16 сентября по московскому времени.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.report import REPORT_FAILED, ParentReporter
from hwcheck.db.repo import Account, HomeworkCounts
from hwcheck.db.reports import SubjectTotals
from onboarding_kit import Kit, actor, make_kit, ready_student

# id MAX и id профилей — приметные числа: по ним ищем утечку в журнале
CHILD, PARENT, YOUNG_PARENT = 5550001, 5550002, 5550003
FIRST_ID = 770000
HEADER = "📈 Отчёт за 7 дней: 10–16 сентября"
NO_CHECKS = (
    f"{HEADER}\n\nПроверок за эти дни не было. Когда ребёнок пришлёт домашку, здесь появятся "
    "задания, ошибки и разборы."
)
SENT_FIELDS = {"kind": "on_request", "component": "report", "user_initiated": True}


def make_family_kit(tmp_path: Path) -> Kit:
    kit = make_kit(tmp_path)
    kit.repo._last_id = FIRST_ID
    return kit


async def parent_of(kit: Kit, user_id: int = PARENT) -> Account:
    account = await kit.repo.get_account(actor(user_id).user_hash)
    assert account is not None
    return account


async def young_child(kit: Kit, grade: int, user_id: int = YOUNG_PARENT) -> int:
    """Ребёнок 1–4 класса с согласием: фото за него присылает родитель."""
    me = actor(user_id)
    parent = await kit.repo.get_or_create_account(me.user_hash, kit.ctx.encrypted_id(me), "parent")
    child = await kit.repo.start_child_by_parent(parent.id, grade, 2026)
    await kit.repo.set_subject(child.id, "math")
    assert await kit.repo.give_parent_consent(child.id, me.user_hash, "v2", kit.clock.now)
    return child.id


async def check(kit: Kit, student_id: int, *counts: int, resolved: int = 0) -> None:
    homework = await kit.repo.add_homework(student_id, "math", HomeworkCounts(*counts))
    assert homework is not None
    for _ in range(resolved):
        await kit.repo.resolve_error(homework.id)


async def request(kit: Kit, user_id: int = PARENT, reports: Any = None) -> None:
    reporter = ParentReporter(kit.ctx, reports if reports is not None else kit.repo)
    await reporter.on_request(actor(user_id), await parent_of(kit, user_id))


class BrokenReports:
    async def totals(
        self, parent_user_id: int, since: datetime, until: datetime
    ) -> list[SubjectTotals]:
        raise ConnectionError(f"database is down for parent {parent_user_id}")


async def test_parent_gets_report_about_his_children(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    older = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    younger = await young_child(kit, 3, PARENT)
    await check(kit, older.id, 12, 9, 2, 1, resolved=1)
    await check(kit, older.id, 11, 9, 1, 1, resolved=1)
    await check(kit, younger, 9, 9, 0, 0)

    await request(kit)

    assert kit.last(PARENT) == (
        f"{HEADER}\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 2 домашки, 23 задания:\n"
        "• верно — 18\n"
        "• с ошибкой — 3, из них разобрал с подсказками — 2\n"
        "• стоит перепроверить — 2\n"
        "\n"
        "Ребёнок (3 класс)\n"
        "Математика — 1 домашка, 9 заданий: все верно ✅",
        None,
    )
    assert kit.last_format(PARENT) is None  # разметки нет: обычный текст
    assert kit.max.to_users == []  # ответ — в чат, где нажата кнопка
    [sent] = kit.events("parent_report_sent")
    assert {key: sent[key] for key in SENT_FIELDS} == SENT_FIELDS
    assert (sent["children"], sent["homeworks"], sent["empty"]) == (2, 3, False)
    assert sent["user"] == actor(PARENT).user_hash
    assert kit.events("parent_report_failed") == []


async def test_report_covers_seven_moscow_days_including_today(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    today = kit.clock.now
    kit.clock.now = datetime(2026, 9, 9, 20, 59, tzinfo=today.tzinfo)  # 9 сентября, 23:59 мск
    await check(kit, child.id, 5, 0, 5, 0)
    kit.clock.now = datetime(2026, 9, 9, 21, 0, tzinfo=today.tzinfo)  # 10 сентября, 00:00 мск
    await check(kit, child.id, 2, 2, 0, 0)
    kit.clock.now = today

    await request(kit)

    text, _ = kit.last(PARENT)
    assert text == (
        f"{HEADER}\n\nРебёнок (7 класс)\nМатематика — 1 домашка, 2 задания: все верно ✅"
    )
    assert kit.events("parent_report_sent")[0]["homeworks"] == 1


async def test_parent_without_checks_is_told_so(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    await request(kit)

    assert kit.last(PARENT) == (NO_CHECKS, None)
    [sent] = kit.events("parent_report_sent")
    assert (sent["children"], sent["homeworks"], sent["empty"]) == (1, 0, True)


async def test_parent_who_sends_photos_himself_is_addressed_directly(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await young_child(kit, 2)
    await young_child(kit, 4)

    await request(kit, YOUNG_PARENT)

    text, _ = kit.last(YOUNG_PARENT)
    assert text == (
        f"{HEADER}\n\nПроверок за эти дни не было. Когда вы пришлёте фото домашки, здесь "
        "появятся задания, ошибки и разборы."
    )
    # отчёт нужен и такому родителю: с проверками он получает те же блоки
    first = (await kit.repo.children((await parent_of(kit, YOUNG_PARENT)).id))[0]
    await check(kit, first.id, 3, 3, 0, 0)
    await request(kit, YOUNG_PARENT)
    assert kit.last(YOUNG_PARENT)[0] == (
        f"{HEADER}\n"
        "\n"
        "Ребёнок (2 класс)\n"
        "Математика — 1 домашка, 3 задания: все верно ✅\n"
        "\n"
        "Ребёнок (4 класс)\n"
        "Проверок за эти дни не было."
    )


async def test_children_without_active_consent_are_left_out(tmp_path: Path) -> None:
    """Согласие отозвано — данные ребёнка в отчёт не идут, даже если проверки были."""
    kit = make_family_kit(tmp_path)
    child = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    await check(kit, child.id, 4, 2, 2, 0)
    [consent] = kit.repo.consents
    consent.revoked_at = kit.clock.now

    await request(kit)

    assert kit.last(PARENT) == (NO_CHECKS, None)
    [sent] = kit.events("parent_report_sent")
    assert (sent["children"], sent["homeworks"], sent["empty"]) == (0, 0, True)


async def test_children_of_same_grade_are_numbered_as_in_notifications(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    first = await young_child(kit, 3)
    second = await young_child(kit, 3)
    await check(kit, second, 2, 2, 0, 0)
    await check(kit, first, 1, 1, 0, 0)

    await request(kit, YOUNG_PARENT)

    assert kit.last(YOUNG_PARENT)[0] == (
        f"{HEADER}\n"
        "\n"
        "Ребёнок 1 (3 класс)\n"
        "Математика — 1 домашка, 1 задание: верно ✅\n"
        "\n"
        "Ребёнок 2 (3 класс)\n"
        "Математика — 1 домашка, 2 задания: все верно ✅"
    )


async def test_storage_failure_gives_apology_and_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    await request(kit, reports=BrokenReports())  # исключение наружу не выходит

    assert kit.last(PARENT) == (REPORT_FAILED, None)
    assert kit.events("parent_report_sent") == []
    [failed] = kit.events("parent_report_failed")
    assert (failed["kind"], failed["error"]) == ("on_request", "ConnectionError")
    assert (failed["component"], failed["user_initiated"]) == ("report", True)
    assert failed["user"] == actor(PARENT).user_hash
    # текст исключения — с id родителя из базы: ни в журнал, ни в лог он не идёт
    assert "database is down" not in json.dumps(kit.events(), ensure_ascii=False)
    assert "database is down" not in caplog.text


async def test_send_failure_does_not_raise(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    async def unavailable(*_args: Any, **_kw: Any) -> None:
        raise TimeoutError("MAX не отвечает")

    kit.max.send_message = unavailable  # type: ignore[method-assign]
    await request(kit)

    assert kit.max.sent == []
    assert kit.events("parent_report_sent") == []
    [failed] = kit.events("parent_report_failed")
    assert (failed["kind"], failed["error"]) == ("on_request", "TimeoutError")


async def test_journal_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт родителю уже ушёл: отказ журнала (диск) не должен стать сбоем апдейта."""
    kit = make_family_kit(tmp_path)
    await ready_student(kit, CHILD, 7, parent_id=PARENT)

    def full_disk(*_args: Any, **_kw: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(kit.ctx.events, "log", full_disk)
    await request(kit)
    assert kit.texts(PARENT) == [NO_CHECKS]  # отчёт доставлен, извинения следом нет


async def test_journal_has_no_max_id_and_no_profile_id(tmp_path: Path) -> None:
    kit = make_family_kit(tmp_path)
    older = await ready_student(kit, CHILD, 7, parent_id=PARENT)
    younger = await young_child(kit, 3, PARENT)
    await check(kit, older.id, 4, 3, 1, 0)
    await check(kit, younger, 2, 2, 0, 0)
    parent = await parent_of(kit)

    await request(kit)
    await request(kit, reports=BrokenReports())

    rows = kit.events("parent_report_sent") + kit.events("parent_report_failed")
    assert len(rows) == 2
    fields = {"ts", "env", "trace_id", "type", "user", "component", "user_initiated", "kind"}
    assert set(rows[0]) == fields | {"children", "homeworks", "empty"}
    assert set(rows[1]) == fields | {"error"}
    assert min(older.id, younger, parent.id) > FIRST_ID  # id из базы — приметные
    for row in rows:
        # время записи — не данные семьи, а в его цифрах id мог бы совпасть случайно
        written = json.dumps({k: v for k, v in row.items() if k != "ts"}, ensure_ascii=False)
        for secret in (PARENT, CHILD, older.id, younger, parent.id):
            assert str(secret) not in written
