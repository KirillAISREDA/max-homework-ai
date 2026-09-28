"""Маршрут вызова по имени модели: `gw:<модель>` — шлюз моделей, без префикса — GigaChat API.

Модель шага задаётся настройкой (`VISION_MODEL=gw:…`), поэтому поставщик меняется без правки
кода шагов. Префикс остаётся в имени модели в журнале и отчёте: по нему видно, через что шёл
вызов.
"""

import contextlib
from collections.abc import AsyncIterator, Sequence

from hwcheck.config import Settings
from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.llm.gateway_client import GatewayClient
from hwcheck.llm.gigachat_client import GigaChatClient
from hwcheck.pipeline.vision import VisionAndChatClient

GATEWAY_PREFIX = "gw:"
MODEL_SETTINGS = ("vision_model", "solver_model", "tutor_model", "lite_model")


class ProviderNotConfigured(RuntimeError):
    """Модель назначена поставщику, который не настроен."""


class ModelRouter:
    """LLMClient и VisionClient: выбирает поставщика по имени модели."""

    def __init__(
        self, *, gigachat: VisionAndChatClient | None, gateway: VisionAndChatClient | None
    ) -> None:
        self._gigachat = gigachat
        self._gateway = gateway

    @property
    def has_gigachat(self) -> bool:
        return self._gigachat is not None

    @property
    def has_gateway(self) -> bool:
        return self._gateway is not None

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult:
        client, name = self._route(model)
        result = await client.chat(messages, model=name, temperature=temperature)
        return result.model_copy(update={"model": model})

    async def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        model: str,
        filename: str = "image.jpg",
    ) -> LLMResult:
        client, name = self._route(model)
        result = await client.analyze_image(image, prompt=prompt, model=name, filename=filename)
        return result.model_copy(update={"model": model})

    def require(self, model: str) -> None:
        """Модель достижима, иначе `ProviderNotConfigured`. Для моделей не из настроек
        (конфигурация стенда): проверить до первого вызова."""
        self._route(model)

    def check(self, settings: Settings) -> None:
        """Каждая модель из настроек достижима; иначе упала бы первая же проверка ребёнка."""
        for field in MODEL_SETTINGS:
            try:
                self.require(getattr(settings, field))
            except ProviderNotConfigured as exc:
                raise ProviderNotConfigured(f"{field.upper()}: {exc}") from exc

    def _route(self, model: str) -> tuple[VisionAndChatClient, str]:
        # значение из .env: регистр префикса и пробелы не должны увести модель шлюза в GigaChat API
        model = model.strip()
        via_gateway = model[: len(GATEWAY_PREFIX)].lower() == GATEWAY_PREFIX
        name = model[len(GATEWAY_PREFIX) :].strip() if via_gateway else model
        if not name:
            raise ProviderNotConfigured(f"имя модели не задано: {model!r}")
        if via_gateway:
            if self._gateway is None:
                raise ProviderNotConfigured(
                    f"модель {model} идёт через шлюз, а он не настроен "
                    "(LLM_GATEWAY_URL, LLM_GATEWAY_KEY)"
                )
            return self._gateway, name
        if self._gigachat is None:
            raise ProviderNotConfigured(
                f"модель {model} идёт в GigaChat API, а он не настроен (GIGACHAT_CREDENTIALS); "
                f"модель шлюза пишется с префиксом «{GATEWAY_PREFIX}»"
            )
        return self._gigachat, model


@contextlib.asynccontextmanager
async def make_llm(settings: Settings) -> AsyncIterator[ModelRouter]:
    """Клиент моделей по настройкам: открыты только настроенные поставщики."""
    async with contextlib.AsyncExitStack() as resources:
        gigachat: VisionAndChatClient | None = None
        gateway: VisionAndChatClient | None = None
        if settings.gigachat_credentials:
            gigachat = await resources.enter_async_context(GigaChatClient(settings))
        if settings.llm_gateway_url or settings.llm_gateway_key:
            gateway = await resources.enter_async_context(
                GatewayClient(
                    settings.llm_gateway_url,
                    settings.llm_gateway_key,
                    timeout=settings.llm_gateway_timeout,
                    max_retries=settings.llm_gateway_max_retries,
                    concurrency=settings.llm_gateway_concurrency,
                )
            )
        router = ModelRouter(gigachat=gigachat, gateway=gateway)
        router.check(settings)
        yield router
