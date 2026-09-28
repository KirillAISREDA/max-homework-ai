"""Маршрут вызова модели по её имени: `gw:<модель>` — шлюз, иначе — GigaChat API."""

from collections.abc import Sequence
from typing import Any

import pytest

from hwcheck.config import Settings
from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.llm.router import GATEWAY_PREFIX, ModelRouter, ProviderNotConfigured, make_llm

MESSAGES = [ChatMessage(role="user", content="тест")]


class Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        self.calls.append(("chat", model))
        return LLMResult(content=self.name, model=model)

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.calls.append(("vision", model))
        return LLMResult(content=self.name, model=model)


async def test_prefix_picks_the_gateway_and_is_cut_from_the_model_name() -> None:
    gigachat, gateway = Recorder("gigachat"), Recorder("gateway")
    router = ModelRouter(gigachat=gigachat, gateway=gateway)

    via_gateway = await router.chat(MESSAGES, model="gw:gigachat-2-max")
    direct = await router.analyze_image(b"x", prompt="p", model="GigaChat-2-Max")

    assert gateway.calls == [("chat", "gigachat-2-max")]
    assert gigachat.calls == [("vision", "GigaChat-2-Max")]
    # в журнал и отчёт модель идёт с префиксом: по нему видно, через что шёл вызов
    assert (via_gateway.content, via_gateway.model) == ("gateway", "gw:gigachat-2-max")
    assert (direct.content, direct.model) == ("gigachat", "GigaChat-2-Max")


@pytest.mark.parametrize(
    ("model", "missing"), [("gw:gigachat-2-max", "LLM_GATEWAY"), ("GigaChat-2-Max", "GIGACHAT")]
)
async def test_missing_provider_is_named_in_the_error(model: str, missing: str) -> None:
    router = ModelRouter(gigachat=None, gateway=None)
    with pytest.raises(ProviderNotConfigured, match=missing):
        await router.chat(MESSAGES, model=model)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


async def test_make_llm_opens_only_configured_providers() -> None:
    both = settings(
        gigachat_credentials="test",
        llm_gateway_url="https://gateway.test/v1",
        llm_gateway_key="sk-test",
        solver_model="gw:gigachat-2-max",
    )
    async with make_llm(both) as router:
        assert router.has_gateway and router.has_gigachat
    async with make_llm(settings(gigachat_credentials="test")) as router:
        assert router.has_gigachat and not router.has_gateway


async def test_make_llm_refuses_models_without_their_provider() -> None:
    """Бот не должен стартовать с моделью, до которой не дотянется: иначе первая же проверка
    ребёнка упадёт."""
    config = settings(gigachat_credentials="test", solver_model=f"{GATEWAY_PREFIX}gigachat-2-max")
    with pytest.raises(ProviderNotConfigured, match="SOLVER_MODEL"):
        async with make_llm(config):
            pass
    with pytest.raises(ProviderNotConfigured, match="VISION_MODEL"):
        async with make_llm(
            settings(llm_gateway_url="https://gateway.test/v1", llm_gateway_key="sk-test")
        ):
            pass


async def test_all_models_on_gateway_need_no_gigachat_credentials() -> None:
    config = settings(
        llm_gateway_url="https://gateway.test/v1",
        llm_gateway_key="sk-test",
        vision_model="gw:v",
        solver_model="gw:s",
        tutor_model="gw:t",
        lite_model="gw:l",
    )
    async with make_llm(config) as router:
        assert router.has_gateway and not router.has_gigachat


@pytest.mark.parametrize(
    "model", ["GW:gigachat-2-max", " gw:gigachat-2-max ", "Gw: gigachat-2-max"]
)
async def test_prefix_survives_case_and_spaces_of_env_file(model: str) -> None:
    """Опечатка в регистре не должна молча увести модель шлюза в GigaChat API."""
    gigachat, gateway = Recorder("gigachat"), Recorder("gateway")
    router = ModelRouter(gigachat=gigachat, gateway=gateway)
    await router.chat(MESSAGES, model=model)
    assert gateway.calls == [("chat", "gigachat-2-max")] and gigachat.calls == []


@pytest.mark.parametrize("model", ["gw:", "gw:  ", "", "   "])
def test_empty_model_name_is_refused(model: str) -> None:
    router = ModelRouter(gigachat=Recorder("gigachat"), gateway=Recorder("gateway"))
    with pytest.raises(ProviderNotConfigured, match="имя модели"):
        router.require(model)


def test_models_outside_settings_are_checked_before_the_run() -> None:
    """Модели стенда заданы его конфигурацией, а не настройками: проверяются до первого вызова."""
    router = ModelRouter(gigachat=Recorder("gigachat"), gateway=None)
    router.require("GigaChat-2-Max")
    with pytest.raises(ProviderNotConfigured, match="LLM_GATEWAY"):
        router.require("gw:gemini-2.5-flash")
