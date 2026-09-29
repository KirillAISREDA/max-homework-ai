"""Бот с итогами родителю (спецификация онбординга §7 п.5, §9.1, §9.4): после сводки ребёнку —
запись `homeworks` и сообщение родителю; сбой уведомления проверку ребёнка не трогает."""

import logging
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot import handlers, notifier
from hwcheck.bot.check import RecognizedPhoto, validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.handlers import RETRY, Bot
from hwcheck.bot.notifier import switch_keyboard
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.config import Settings
from hwcheck.db.repo import HomeworkCounts
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession
from hwcheck.pipeline.vision import RecognizedPage
from onboarding_kit import Kit, actor, chat, make_kit, photo, press, ready_student, text

CHILD, PARENT = 1, 101
ONE_ERROR = [(16, "2 + 2 = 4"), (17, "3 + 3 = 6"), (18, "5 + 5 = 10"), (19, "2 + 2 = 5")]
CHECKED = (
    "Ребёнок (7 класс) проверил домашку по математике: 4 задания — 3 верно, "
    "1 с ошибкой (№19), разбирает с подсказками."
)
RESOLVED = "Ребёнок (7 класс) разобрал все ошибки в домашке по математике ✅"


def notebook(lines: list[tuple[int, str]]) -> VisionPage:
    """Страница тетради: у заданий нет условия — солвер (LLM) не зовётся, считает валидатор."""
    tasks = [
        VisionTask(number=number, task_text="", student_solution_steps=[line], confidence=1)
        for number, line in lines
    ]
    return VisionPage(tasks=tasks, page_ok=True)


def make_bot(
    tmp_path: Path,
    lines: list[tuple[int, str]] | None = ONE_ERROR,
    *,
    onboarding: bool = True,
    reports: bool = False,
) -> tuple[Bot, Kit]:
    """`lines=None` — распознавание не подменено: без LLM проверка падает (`check_failed`).
    `reports` — у онбординга есть хранилище отчёта родителю, как в prod."""
    kit = make_kit(tmp_path)
    bot = Bot(
        kit.max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        kit.ctx.dialogs,
        kit.ctx.events,
        Settings(_env_file=None),
        onboarding=Onboarding(kit.ctx, kit.repo if reports else None) if onboarding else None,
    )
    if lines is not None:
        page = notebook(lines)

        async def recognize(user_id: int | None, image: bytes, path: str | None) -> Any:
            rec = RecognizedPage(
                page=page, orientation=0, attempts=0, tokens_in=0, tokens_out=0, latency_s=0.0,
                raw="",
            )  # fmt: skip
            return RecognizedPhoto(page=page, role="notebook" if page.tasks else "empty", rec=rec)

        bot._recognize = recognize  # type: ignore[method-assign]
    return bot, kit


def event_types(kit: Kit) -> list[str]:
    return [e["type"] for e in kit.events()]


async def resolve_error(
    bot: Bot, kit: Kit, monkeypatch: pytest.MonkeyPatch, index: int, user_id: int = CHILD
) -> None:
    """Ребёнок дошёл до верного ответа в разборе задания `index`: тьютор подменён."""
    session = TutorSession(
        task_text="", student_steps=[], student_answer=None, ref=RefSolution(steps=[], answer="4")
    )

    async def solved(_llm: Any, current: TutorSession, _text: str, **_kw: Any) -> Any:
        return "Верно! 🎉", current.model_copy(update={"resolved": True})

    monkeypatch.setattr(handlers, "tutor_reply", solved)
    state = await kit.ctx.dialogs.get(chat(user_id))
    tutoring = {"phase": "tutoring", "tutor": session, "tutoring_index": index}
    await kit.ctx.dialogs.set(chat(user_id), state.model_copy(update=tutoring))
    await bot.handle_update(text(user_id, "4"))


async def test_parent_is_told_after_child_summary(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    profile = await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.max.to_users == [(PARENT, CHECKED, switch_keyboard(enabled=True))]
    order = [(kind, who) for kind, who, _ in kit.max.timeline]
    assert order == [("chat", chat(CHILD)), ("chat", chat(CHILD)), ("user", PARENT)]
    assert kit.max.timeline[1][2].startswith("Проверил! 3 из 4 верно.")  # сначала сводка ребёнку

    [homework] = kit.repo.homeworks.values()
    assert (homework.student_id, homework.subject) == (profile.id, "math")
    assert homework.counts == HomeworkCounts(total=4, correct=3, wrong=1, uncertain=0)
    assert (await kit.ctx.dialogs.get(chat(CHILD))).homework_id == homework.id
    [sent] = kit.events("notify_sent")
    assert (sent["kind"], sent["component"]) == ("homework_checked", "notifier")
    # в журнале — обезличенный инициатор (ребёнок), id MAX родителя туда не попадает
    assert sent["user"] == actor(CHILD).user_hash and sent["user_initiated"] is False
    assert "update_failed" not in event_types(kit)


async def test_notification_offers_report_and_report_counts_the_check(tmp_path: Path) -> None:
    """Под итогом — вторая строка «Отчёт о прогрессе»; проверка из итога входит в отчёт."""
    bot, kit = make_bot(tmp_path, reports=True)
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    keyboard = switch_keyboard(enabled=True, report=True)
    assert kit.max.to_users == [(PARENT, CHECKED, keyboard)]
    assert [[button["payload"] for button in row] for row in keyboard] == [
        ["ob:notify:off"],
        ["ob:report"],
    ]

    await bot.handle_update(press(PARENT, "ob:report"))
    report, buttons = kit.last(PARENT)
    assert report == (
        "📈 Отчёт за 7 дней: 10–16 сентября\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 1 домашка, 4 задания:\n"
        "• верно — 3\n"
        "• с ошибкой — 1"
    )
    assert buttons is None
    for leaked in ("2 + 2", "= 5", "№19"):  # ни решений, ни номеров заданий
        assert leaked not in report
    assert "update_failed" not in event_types(kit)


async def test_parent_who_blocked_the_bot_does_not_break_the_check(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    kit.max.blocked_users.add(PARENT)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    summary, buttons = kit.last(CHILD)
    assert summary.startswith("Проверил! 3 из 4 верно.") and buttons is not None
    assert RETRY not in kit.texts(CHILD)
    [failed] = kit.events("notify_failed")
    assert (failed["kind"], failed["error"]) == ("homework_checked", "RuntimeError")
    assert failed["component"] == "notifier" and failed["user"] == actor(CHILD).user_hash
    types = event_types(kit)
    assert "check_failed" not in types and "update_failed" not in types
    assert "notify_sent" not in types
    assert len(kit.repo.homeworks) == 1  # учёт проверки от уведомления не зависит
    assert kit.max.to_users == []  # повторов нет


async def test_mode_off_keeps_the_record_but_sends_nothing(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    parent = await kit.repo.get_account(actor(PARENT).user_hash)
    assert parent is not None
    await kit.repo.set_notify_mode(parent.id, "off")
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.max.to_users == []
    assert len(kit.repo.homeworks) == 1
    types = event_types(kit)
    assert "notify_sent" not in types and "notify_failed" not in types


async def test_digest_mode_is_sent_at_once_until_digest_exists(tmp_path: Path) -> None:
    """Сводки по расписанию ещё нет: «digest» не должен молча оставить родителя без итогов."""
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    parent = await kit.repo.get_account(actor(PARENT).user_hash)
    assert parent is not None
    await kit.repo.set_notify_mode(parent.id, "digest")
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    assert [(who, message) for who, message, _ in kit.max.to_users] == [(PARENT, CHECKED)]


@pytest.mark.parametrize("missing", ["link", "account"])
async def test_child_without_parent_in_database(tmp_path: Path, missing: str) -> None:
    """Родитель удалён (`ON DELETE SET NULL`), согласие осталось: проверка идёт, писать некому."""
    bot, kit = make_bot(tmp_path)
    profile = await ready_student(kit)
    stored = kit.repo._profiles[profile.id]
    if missing == "link":
        stored.parent_user_id = None
    else:
        del kit.repo._accounts[actor(PARENT).user_hash]
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.last(CHILD)[0].startswith("Проверил! 3 из 4 верно.")
    assert kit.max.to_users == [] and len(kit.repo.homeworks) == 1
    types = event_types(kit)
    assert not {"notify_sent", "notify_failed", "update_failed", "check_failed"} & set(types)


async def test_young_child_checked_by_parent_needs_no_notification(tmp_path: Path) -> None:
    """1–4 класс: фото присылает родитель и результат видит сам — но проверка учитывается."""
    bot, kit = make_bot(tmp_path)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    await bot.handle_update(photo(2, "https://files/2.jpg"))

    assert kit.last(2)[0].startswith("Проверил! 3 из 4 верно.")
    assert kit.max.to_users == []
    parent = await kit.repo.get_account(actor(2).user_hash)
    assert parent is not None
    [child] = await kit.repo.children(parent.id)
    [homework] = kit.repo.homeworks.values()
    assert homework.student_id == child.id
    types = event_types(kit)
    assert "notify_sent" not in types and "notify_failed" not in types


async def test_second_young_child_gets_own_record(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    for payload in ("ob:addchild", "ob:pgrade:4", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    parent = await kit.repo.get_account(actor(2).user_hash)
    assert parent is not None
    _second_grade, fourth_grade = await kit.repo.children(parent.id)

    await bot.handle_update(photo(2, "https://files/2.jpg"))
    assert kit.repo.homeworks == {}  # «Чья домашка?» — проверки ещё не было
    await bot.handle_update(press(2, f"ob:whose:{fourth_grade.id}"))
    await bot.handle_update(photo(2, "https://files/3.jpg"))  # выбор действует час
    assert [h.student_id for h in kit.repo.homeworks.values()] == [fourth_grade.id] * 2
    assert kit.max.to_users == []


async def test_children_of_same_grade_are_numbered(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit, user_id=1, parent_id=PARENT)
    await ready_student(kit, user_id=3, grade=5, parent_id=PARENT)
    await ready_student(kit, user_id=4, parent_id=PARENT)

    for child in (4, 1, 3):
        await bot.handle_update(photo(child, "https://files/1.jpg"))
    told = [(who, message.split(" проверил")[0]) for who, message, _ in kit.max.to_users]
    assert told == [
        (PARENT, "Ребёнок 2 (7 класс)"),
        (PARENT, "Ребёнок 1 (7 класс)"),
        (PARENT, "Ребёнок (5 класс)"),
    ]


@pytest.mark.parametrize("lines", [None, []])
async def test_failed_or_unreadable_check_leaves_no_trace(
    tmp_path: Path, lines: list[tuple[int, str]] | None
) -> None:
    """Проверка упала или ни одно фото не распознано — ни записи, ни уведомления."""
    bot, kit = make_bot(tmp_path, lines)
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.repo.homeworks == {} and kit.max.to_users == []
    assert ("check_failed" in event_types(kit)) == (lines is None)
    assert (await kit.ctx.dialogs.get(chat(CHILD))).homework_id is None


async def test_uncertain_task_is_reported_as_worth_rechecking(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path, [(5, "2 + 2 = 4"), (6, "15 <неразборчиво> 5 = 200")])
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    [(who, message, _)] = kit.max.to_users
    assert who == PARENT
    assert message.endswith("2 задания — 1 верно, 1 стоит перепроверить.")
    assert "ошиб" not in message
    [homework] = kit.repo.homeworks.values()
    assert homework.counts == HomeworkCounts(total=2, correct=1, wrong=0, uncertain=1)
    # вопрос ребёнку задан до уведомления родителю и пережил запись номера проверки
    state = await kit.ctx.dialogs.get(chat(CHILD))
    assert state.homework_id == homework.id
    assert kit.max.timeline[-1][0] == "user"


async def test_database_failure_does_not_take_back_the_summary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)

    async def broken(*_args: Any, **_kw: Any) -> Any:
        raise ConnectionError("postgres down")

    kit.repo.add_homework = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="hwcheck.bot.notifier"):
        await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.last(CHILD)[0].startswith("Проверил! 3 из 4 верно.")
    assert RETRY not in kit.texts(CHILD)
    [failed] = kit.events("homework_save_failed")
    assert (failed["error"], failed["component"]) == ("ConnectionError", "notifier")
    # только журнал уведомлений: у шага похвалы в этом боте нет модели, и он пишет своё
    notifier_log = [r for r in caplog.records if r.name == "hwcheck.bot.notifier"]
    assert [r.levelno for r in notifier_log] == [logging.WARNING]
    assert "update_failed" not in event_types(kit)
    assert (await kit.ctx.dialogs.get(chat(CHILD))).homework_id is None
    assert [message for _, message, _ in kit.max.to_users] == [CHECKED]  # итог родителю ушёл


@pytest.mark.parametrize("broken_step", ["summarize", "checked_text", "child_label"])
async def test_notifier_bug_never_reaches_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_step: str
) -> None:
    """Сбой не в базе, а в самом коде уведомления (подсчёт, текст): ребёнок сводку уже получил —
    «попробуй ещё раз» после неё было бы неправдой (ревью 28.09)."""
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)

    def broken(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("bug in notifier")

    monkeypatch.setattr(notifier, broken_step, broken)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.last(CHILD)[0].startswith("Проверил! 3 из 4 верно.")
    assert RETRY not in kit.texts(CHILD)
    types = event_types(kit)
    assert "update_failed" not in types and "check_failed" not in types
    failures = [e for e in kit.events() if e["type"] in ("notifier_failed", "notify_failed")]
    assert [e["error"] for e in failures] == ["RuntimeError"]
    assert failures[0]["component"] == "notifier"
    assert kit.max.to_users == []


@pytest.mark.parametrize("broken_step", ["remaining_buttons", "resolved_text"])
async def test_notifier_bug_does_not_break_the_tutoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_step: str
) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    def broken(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("bug in notifier")

    monkeypatch.setattr(notifier, broken_step, broken)
    await resolve_error(bot, kit, monkeypatch, index=3)

    assert kit.last(CHILD)[0] == "Верно! 🎉"
    assert RETRY not in kit.texts(CHILD)
    assert "update_failed" not in event_types(kit)
    [failed] = kit.events("notifier_failed")
    assert (failed["error"], failed["component"]) == ("RuntimeError", "notifier")
    assert len(kit.max.to_users) == 1  # только итог проверки


async def test_unwritable_event_log_does_not_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой базы и следом сбой записи события о нём: защита не должна падать сама."""
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)

    async def broken(*_args: Any, **_kw: Any) -> Any:
        raise ConnectionError("postgres down")

    kit.repo.add_homework = broken  # type: ignore[method-assign]
    kit.repo.get_profile = broken  # type: ignore[method-assign]
    log = kit.ctx.events.log

    def picky_log(event_type: str, **fields: Any) -> None:
        if event_type in ("homework_save_failed", "notify_failed", "notifier_failed"):
            raise OSError("disk full")
        log(event_type, **fields)

    monkeypatch.setattr(kit.ctx.events, "log", picky_log)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.last(CHILD)[0].startswith("Проверил! 3 из 4 верно.")
    assert RETRY not in kit.texts(CHILD)
    assert "update_failed" not in event_types(kit)


async def test_parent_lookup_failure_is_a_failed_notification(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)

    async def broken(*_args: Any, **_kw: Any) -> Any:
        raise ConnectionError("postgres down")

    kit.repo.account_by_id = broken  # type: ignore[method-assign]
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))

    assert kit.last(CHILD)[0].startswith("Проверил! 3 из 4 верно.")
    [failed] = kit.events("notify_failed")
    assert (failed["kind"], failed["error"]) == ("homework_checked", "ConnectionError")
    assert kit.max.to_users == [] and "update_failed" not in event_types(kit)


async def test_parent_is_told_once_when_all_errors_are_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [(16, "2 + 2 = 4"), (19, "2 + 2 = 5"), (20, "3 + 3 = 7")]
    bot, kit = make_bot(tmp_path, lines)
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    assert len(kit.max.to_users) == 1
    [homework] = kit.repo.homeworks.values()

    await resolve_error(bot, kit, monkeypatch, index=1)
    assert len(kit.max.to_users) == 1  # одна ошибка ещё не разобрана — родителю не пишем
    assert kit.repo.homeworks[homework.id].errors_resolved == 1
    assert kit.events("homework_resolved") == []

    await resolve_error(bot, kit, monkeypatch, index=2)
    assert kit.max.to_users[1] == (PARENT, RESOLVED, switch_keyboard(enabled=True))
    assert kit.repo.homeworks[homework.id].errors_resolved == 2
    assert [e["kind"] for e in kit.events("notify_sent")] == ["homework_checked", "errors_resolved"]
    [resolved] = kit.events("homework_resolved")
    assert (resolved["subject"], resolved["errors"]) == ("math", 2)
    # ответ тьютора ребёнку ушёл раньше сообщения родителю
    assert kit.max.timeline[-2:] == [("chat", chat(CHILD), "Верно! 🎉"), ("user", PARENT, RESOLVED)]

    # кнопка «Разобрать» в старой сводке жива: повторный разбор — не второе «разобрал все ошибки»
    await resolve_error(bot, kit, monkeypatch, index=2)
    assert len(kit.max.to_users) == 2
    assert kit.repo.homeworks[homework.id].errors_resolved == 2
    assert len(kit.events("homework_resolved")) == 1
    assert len(kit.events("error_fixed")) == 3


async def test_resolved_errors_with_blocked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    kit.max.blocked_users.add(PARENT)
    await resolve_error(bot, kit, monkeypatch, index=3)

    assert kit.last(CHILD)[0] == "Верно! 🎉"
    [failed] = kit.events("notify_failed")
    assert (failed["kind"], failed["error"]) == ("errors_resolved", "RuntimeError")
    assert len(kit.events("homework_resolved")) == 1  # метрика от доставки не зависит
    assert "update_failed" not in event_types(kit)


async def test_young_child_resolved_errors_are_counted_without_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, kit = make_bot(tmp_path)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    await bot.handle_update(photo(2, "https://files/2.jpg"))
    await resolve_error(bot, kit, monkeypatch, index=3, user_id=2)

    [homework] = kit.repo.homeworks.values()
    assert homework.errors_resolved == 1 and kit.max.to_users == []
    assert len(kit.events("homework_resolved")) == 1


async def test_dialog_started_before_deploy_has_no_homework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Состояние Redis без `homework_id` (проверка до выката): разбор идёт как раньше."""
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    steps = ["2 + 2 = 5"]
    task = VisionTask(number=19, task_text="", student_solution_steps=steps, confidence=1)
    wrong = CheckedTask(task=task, ref=None, grade=validator_only_grade(steps))
    old = ChatState(phase="review", tasks=[wrong]).model_dump(exclude={"homework_id"})
    await kit.ctx.dialogs.set(chat(CHILD), ChatState.model_validate(old))

    await resolve_error(bot, kit, monkeypatch, index=0)
    assert kit.last(CHILD)[0] == "Верно! 🎉"
    assert kit.max.to_users == [] and kit.repo.homeworks == {}
    types = event_types(kit)
    assert "error_fixed" in types
    assert not {"homework_resolved", "homework_save_failed", "update_failed"} & set(types)


async def test_without_onboarding_bot_works_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ONBOARDING_REQUIRED=false`: ни учёта, ни уведомлений — и ни одного обращения к базе."""
    bot, kit = make_bot(tmp_path, onboarding=False)
    profile = await ready_student(kit)  # профиль в базе есть, но боту до него нет дела

    def forbidden(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("без онбординга бот не ходит в базу профилей")

    for name in ("get_profile", "add_homework", "resolve_error", "notify_mode", "get_account"):
        monkeypatch.setattr(kit.repo, name, forbidden)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    summary, buttons = kit.last(CHILD)
    assert summary.startswith("Проверил! 3 из 4 верно.") and buttons is not None
    await resolve_error(bot, kit, monkeypatch, index=3)

    assert kit.last(CHILD)[0] == "Верно! 🎉"
    assert kit.max.to_users == [] and kit.repo.homeworks == {}
    assert (await kit.ctx.dialogs.get(chat(CHILD))).homework_id is None
    assert profile.parent_user_id is not None
    types = set(event_types(kit))
    assert not {"notify_sent", "notify_failed", "homework_resolved", "update_failed"} & types
