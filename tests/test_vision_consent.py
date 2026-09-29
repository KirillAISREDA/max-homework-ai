"""Модель чтения фото выбирается по согласию родителя.

Сторонняя модель — трансграничная передача фото тетради. О ней родителю говорит политика v2: фото
семьи, чьё согласие дано по прежней политике (или вовсе без онбординга), читает GigaChat.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.check import CheckModels, recognize_photo, vision_of, vision_override
from hwcheck.bot.handlers import Bot, models_for
from hwcheck.bot.onboarding.policy import POLICY_VERSION, allows_foreign_models
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding
from hwcheck.config import Settings
from hwcheck.llm.journal import JournaledLLM
from hwcheck.subjects.russian.module import RussianModule
from onboarding_kit import Kit, make_kit, photo, ready_student
from test_ru_gaps import WORDS
from test_ru_recognize import TEXTBOOK
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


# --- модель по умолчанию — отечественная: путь, забывший о согласии, безопасен (ревью 29.09) ---


class ModelSpy(FakeTwoStageClient):
    """Запоминает, какой модели ушло фото."""

    def __init__(self, vision: list[str], chats: list[str]) -> None:
        super().__init__(vision, chats)
        self.vision_models: list[str] = []

    async def analyze_image(self, image: bytes, **kwargs: Any) -> Any:
        self.vision_models.append(kwargs["model"])
        return await super().analyze_image(image, **kwargs)


def test_models_of_settings_read_photo_by_domestic_model() -> None:
    """Модели, собранные один раз при старте бота (`SubjectDeps`), о согласии не знают."""
    settings = Settings(_env_file=None, vision_model=FOREIGN, vision_model_domestic=DOMESTIC)
    assert models_for(settings).vision == DOMESTIC


async def test_any_photo_reader_obeys_consent_of_the_check() -> None:
    models = CheckModels(vision=DOMESTIC, structure="s", solver="m")
    spy = ModelSpy([TRANSCRIPT, TRANSCRIPT], [STRUCTURED, STRUCTURED])

    await recognize_photo(spy, make_image(), models)
    with vision_override(FOREIGN):
        await recognize_photo(spy, make_image(), models)
    with vision_override(None):
        assert vision_of(models) == DOMESTIC

    assert spy.vision_models == [DOMESTIC, FOREIGN]


@pytest.mark.parametrize(("override", "expected"), [(None, DOMESTIC), (FOREIGN, FOREIGN)])
async def test_language_module_obeys_consent_of_the_check(
    override: str | None, expected: str
) -> None:
    """Модуль предмета создаётся при старте бота с моделями из настроек: без этого теста фото
    русского языка уходило сторонней модели у всех семей."""
    spy = ModelSpy([TEXTBOOK], [])
    models = CheckModels(vision=DOMESTIC, structure="s", solver="m")
    module = RussianModule(spy, models, ocr=None, dictionary=WORDS)
    with vision_override(override):
        await module.recognize(make_image())
    assert spy.vision_models == [expected]
