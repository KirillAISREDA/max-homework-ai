"""Бот с онбордингом (спецификация §7 «Встраивание», §10.2): до согласия фото не скачивается,
фото с выбранным ребёнком и апдейты готового ученика идут в проверку."""

from pathlib import Path

from hwcheck.bot.handlers import WELCOME, Bot
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.config import Settings
from onboarding_kit import Kit, make_kit, photo, press, ready_student, text


def make_bot(tmp_path: Path) -> tuple[Bot, Kit]:
    kit = make_kit(tmp_path)
    bot = Bot(
        kit.max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]  # без LLM распознавание упадёт: важен маршрут
        kit.ctx.dialogs,
        kit.ctx.events,
        Settings(_env_file=None),
        onboarding=Onboarding(kit.ctx),
    )
    return bot, kit


async def test_photo_before_consent_is_not_downloaded(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await bot.handle_update(photo(1, "https://files/1.jpg"))
    assert kit.max.downloads == []
    assert kit.last(1)[0] == texts.HELLO
    types = [e["type"] for e in kit.events()]
    assert "photo_blocked_no_consent" in types and "homework_uploaded" not in types


async def test_parent_photo_with_young_child_goes_to_check(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    await bot.handle_update(photo(2, "https://files/2.jpg"))
    assert kit.max.downloads == ["https://files/2.jpg"]
    checks = [e["type"] for e in kit.events() if e["type"] in ("homework_uploaded", "check_failed")]
    assert checks == ["homework_uploaded", "check_failed"]


async def test_ready_student_text_reaches_check_dialog(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    await bot.handle_update(text(1, "привет"))
    assert kit.last(1)[0] == WELCOME
