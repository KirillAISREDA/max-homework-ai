"""Реализация LLMClient/VisionClient поверх официального SDK `gigachat`.

OAuth (токен на 30 минут) и его обновление SDK делает сам; сертификат Минцифры
подключается через ca_bundle_file (см. .env.example).
"""

import asyncio
import contextlib
import time
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

from gigachat import GigaChat

from hwcheck.config import Settings
from hwcheck.llm.base import ChatMessage, LLMResult


class GigaChatClient:
    def __init__(self, settings: Settings) -> None:
        self._client = GigaChat(
            credentials=settings.gigachat_credentials,
            scope=settings.gigachat_scope,
            verify_ssl_certs=settings.gigachat_verify_ssl_certs,
            ca_bundle_file=settings.gigachat_ca_bundle,
            timeout=settings.gigachat_timeout,
            max_retries=settings.gigachat_max_retries,
        )
        # тариф ограничивает одновременные запросы (PERS: 1) — иначе 429.
        # Семафор берётся на один HTTP-вызов, не на составную операцию (без вложенности)
        self._semaphore = asyncio.Semaphore(settings.gigachat_concurrency)

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
        }
        return await self._call(payload, model=model)

    async def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        model: str,
        filename: str = "image.jpg",
    ) -> LLMResult:
        t0 = time.monotonic()
        async with self._semaphore:
            uploaded = await self._client.aupload_file((filename, image, _mime_type(filename)))
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt, "attachments": [uploaded.id_]}],
            "temperature": 0.1,
        }
        try:
            result = await self._call(payload, model=model)
        finally:
            # 152-ФЗ: фото тетради не должно оставаться в хранилище GigaChat.
            # Ошибка удаления не важнее основного результата/ошибки.
            with contextlib.suppress(Exception):
                async with self._semaphore:
                    await self._client.adelete_file(uploaded.id_)
        result.latency_s = time.monotonic() - t0  # включая upload файла
        return result

    async def _call(self, payload: dict[str, Any], *, model: str) -> LLMResult:
        t0 = time.monotonic()
        async with self._semaphore:
            response = await self._client.achat(payload)
        latency = time.monotonic() - t0
        usage = response.usage
        return LLMResult(
            content=response.choices[0].message.content or "",
            model=model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            latency_s=latency,
        )


def _mime_type(filename: str) -> str:
    return "image/png" if filename.lower().endswith(".png") else "image/jpeg"
