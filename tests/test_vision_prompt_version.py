"""Промпт чтения фото следует за моделью: `vision/v3` писался под GigaChat, сторонняя модель
шлюза получает свой (`v4-<модель>`). Версия задаётся конфигурацией стенда и настройками бота и
переезжает вместе с моделью, назначенной проверке по согласию родителя (`vision_override`)."""

from pathlib import Path

import pytest

from hwcheck.bench.runner import BenchConfig
from hwcheck.bot.check import (
    CheckModels,
    recognize_photo,
    vision_of,
    vision_override,
    vision_prompt_of,
)
from hwcheck.bot.handlers import models_for
from hwcheck.config import Settings
from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.loadtest import as_consented
from hwcheck.prompts import PROMPTS_DIR, load_prompt
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, make_image

DOMESTIC = "GigaChat-2-Max"
FOREIGN = "gw:gemini-3.1-pro-preview"


class PromptSpy:
    """Запоминает, с каким промптом и моделью ушло каждое фото."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.calls.append((model, prompt))
        return LLMResult(content=TRANSCRIPT, model=model, tokens_in=1, tokens_out=1)

    async def chat(
        self, messages: list[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        return LLMResult(content=STRUCTURED, model=model, tokens_in=1, tokens_out=1)


def test_default_prompt_version_is_v3_everywhere() -> None:
    assert CheckModels(vision="v", structure="s", solver="m").vision_prompt == "v3"
    config = BenchConfig(name="x", vision_model="v", structure_model="s", solver_model="m")
    assert config.models.vision_prompt == "v3"
    settings = Settings(_env_file=None)
    assert settings.vision_prompt == "v3" and settings.vision_prompt_domestic == "v3"


def test_bench_config_carries_prompt_version_into_models() -> None:
    config = BenchConfig(
        name="gw-gemini31pro-v4",
        vision_model=FOREIGN,
        structure_model="s",
        solver_model="m",
        vision_prompt="v4-gemini",
    )
    assert config.models.vision_prompt == "v4-gemini"


async def test_recognize_photo_reads_with_configured_prompt_version() -> None:
    spy = PromptSpy()
    models = CheckModels(vision=FOREIGN, structure="s", solver="m", vision_prompt="v4-gemini")
    await recognize_photo(spy, make_image(), models)
    assert spy.calls == [(FOREIGN, load_prompt("vision", "v4-gemini"))]


async def test_prompt_version_follows_the_model_assigned_by_consent() -> None:
    """Согласие v2 назначает проверке стороннюю модель — и её промпт; без согласия фото читает
    отечественная модель своим промптом. Модель без своего промпта осталась бы на v3."""
    spy = PromptSpy()
    models = CheckModels(vision=DOMESTIC, structure="s", solver="m")

    with vision_override(FOREIGN, prompt="v4-gemini"):
        assert vision_of(models) == FOREIGN
        assert vision_prompt_of(models) == "v4-gemini"
        await recognize_photo(spy, make_image(), models)
    with vision_override(None, prompt="v4-gemini"):
        assert vision_of(models) == DOMESTIC
        assert vision_prompt_of(models) == "v3"
    await recognize_photo(spy, make_image(), models)

    assert spy.calls == [
        (FOREIGN, load_prompt("vision", "v4-gemini")),
        (DOMESTIC, load_prompt("vision", "v3")),
    ]


def test_settings_prompt_versions_reach_models_and_override() -> None:
    settings = Settings(
        _env_file=None,
        vision_model=FOREIGN,
        vision_model_domestic=DOMESTIC,
        vision_prompt="v4-gemini",
        vision_prompt_domestic="v3",
    )
    models = models_for(settings)
    assert (models.vision, models.vision_prompt) == (DOMESTIC, "v3")


@pytest.mark.parametrize("version", ["v3", "v4-gemini", "v4-claude"])
def test_vision_prompts_exist_and_forbid_fixing_the_student(version: str) -> None:
    """Инвариант для любой модели чтения: переписывать как есть, не исправлять и не решать."""
    path = Path(PROMPTS_DIR) / "vision" / f"{version}.md"
    text = path.read_text(encoding="utf-8")
    assert "не исправляй" in text.lower() or "никогда не исправляй" in text.lower()
    assert "<неразборчиво>" in text


@pytest.mark.parametrize("field", ["vision_prompt", "vision_prompt_domestic"])
@pytest.mark.parametrize("version", ["v9-missing", "../solver/v1", "", "V3", "v3.md"])
def test_unknown_prompt_version_stops_the_bot_at_start(field: str, version: str) -> None:
    """Опечатка в .env иначе всплыла бы на первом фото ребёнка: «попробуй ещё раз» на каждое."""
    with pytest.raises(ValueError, match="prompts/vision"):
        Settings(_env_file=None, **{field: version})


def test_load_test_reads_pages_with_the_prompt_of_the_foreign_model() -> None:
    settings = Settings(
        _env_file=None,
        vision_model=FOREIGN,
        vision_model_domestic=DOMESTIC,
        vision_prompt="v4-gemini",
    )
    models = models_for(as_consented(settings))
    assert (models.vision, models.vision_prompt) == (FOREIGN, "v4-gemini")
