"""Модель чтения фото выбирается по согласию родителя.

Сторонняя модель — трансграничная передача фото тетради. О ней родителю говорит политика v2: фото
семьи, чьё согласие дано по прежней политике (или вовсе без онбординга), читает GigaChat.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.handlers import Bot
from hwcheck.bot.onboarding.policy import POLICY_VERSION, allows_foreign_models
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding
from hwcheck.config import Settings
from hwcheck.llm.journal import JournaledLLM
from onboarding_kit import Kit, make_kit, photo, ready_student
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, FakeTwoStageClient, make_image

FOREIGN = "gw:gemini-3.1-pro-preview"
DOMESTIC = "GigaChat-2-Max"
CHILD = 1


@pytest.mark.parametrize(
    ("version", "allowed"),
    [("v2", True), ("v3", True), ("v10", True), ("v1", False), ("v0", False), (None, False)],
)
def test_foreign_models_are_allowed_since_policy_v2(version: str | None, allowed: bool) -> None:
    assert allows_foreign_models(version) is allowed


@pytest.mark.parametrize("version", ["", "2", "v", "v2-draft", "latest", "V2", " v2"])
def test_unknown_policy_version_allows_nothing(version: str) -> None:
    """Непонятная запись версии — не согласие на передачу за рубеж."""
    assert allows_foreign_models(version) is False


def test_current_policy_tells_about_foreign_model() -> None:
    assert allows_foreign_models(POLICY_VERSION)


def make_bot(tmp_path: Path, *, onboarding: bool = True) -> tuple[Bot, Kit]:
    kit = make_kit(tmp_path)

    class MaxWithPhoto(type(kit.max)):  # type: ignore[misc]
        async def download(self, url: str) -> bytes:
            return make_image()

    kit.max.__class__ = MaxWithPhoto
    inner = FakeTwoStageClient([TRANSCRIPT], [STRUCTURED, json.dumps({"items": []})])
    bot = Bot(
        kit.max,  # type: ignore[arg-type]
        JournaledLLM(inner, kit.ctx.events),
        kit.ctx.dialogs,
        kit.ctx.events,
        Settings(
            _env_file=None, photos_ttl_days=0, vision_model=FOREIGN, vision_model_domestic=DOMESTIC
        ),
        onboarding=Onboarding(kit.ctx) if onboarding else None,
    )
    return bot, kit


def vision_models(kit: Kit) -> list[str]:
    return [e["model"] for e in kit.events("llm_call") if e["kind"] == "vision"]


async def test_consent_by_current_policy_sends_photo_to_foreign_model(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit, policy=POLICY_VERSION)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    assert vision_models(kit) == [FOREIGN]


@pytest.mark.parametrize("old", ["v0", "v1"])
async def test_consent_by_old_policy_keeps_photo_on_gigachat(tmp_path: Path, old: str) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit, policy=old)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    assert vision_models(kit) == [DOMESTIC]


async def test_without_onboarding_photo_stays_on_gigachat(tmp_path: Path) -> None:
    """Аварийный режим без онбординга: согласия нет вовсе — сторонней модели фото не уходит."""
    bot, kit = make_bot(tmp_path, onboarding=False)
    await bot.handle_update(photo(CHILD, "https://files/1.jpg"))
    assert vision_models(kit) == [DOMESTIC]


async def test_check_photos_carry_policy_of_consent(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile = await ready_student(kit, policy=POLICY_VERSION)
    assert profile.consent_policy == POLICY_VERSION
    route: Any = await Onboarding(kit.ctx).route(photo(CHILD, "https://files/1.jpg"))
    assert route == CheckPhotos(
        ["https://files/1.jpg"],
        subject="math",
        student_id=profile.id,
        policy_version=POLICY_VERSION,
    )
