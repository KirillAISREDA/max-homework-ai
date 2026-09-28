"""Реализация LLMClient/VisionClient поверх шлюза моделей с OpenAI-совместимым API.

Шлюз выдают организаторы конкурса: один адрес и ключ, десятки моделей. Изображение уходит в
самом запросе (data URL) — загрузки файлов, как в родном API GigaChat, у шлюза нет. Стоимость
вызова шлюз отдаёт заголовком ответа: она точнее пересчёта токенов по тарифу.
"""

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from hwcheck.llm.base import ChatMessage, LLMResult

logger = logging.getLogger(__name__)

COST_HEADER = "x-litellm-response-cost"
# перегрузка и сбой шлюза проходят сами; ошибка запроса (4xx) повтором не лечится
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
FIRST_PAUSE_S = 1.0
ERROR_TEXT_LIMIT = 300


class GatewayError(RuntimeError):
    """Шлюз не ответил или отказал. `status_code` — код ответа; None — не было связи."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 90.0,
        max_retries: int = 3,
        concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("адрес шлюза моделей не задан (LLM_GATEWAY_URL)")
        if not api_key:
            raise ValueError("ключ шлюза моделей не задан (LLM_GATEWAY_KEY)")
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult:
        return await self._call(model, [m.model_dump() for m in messages], temperature)

    async def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        model: str,
        filename: str = "image.jpg",
    ) -> LLMResult:
        encoded = base64.b64encode(image).decode("ascii")
        url = f"data:{_mime_type(filename)};base64,{encoded}"
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": url}},
        ]
        return await self._call(model, [{"role": "user", "content": content}], 0.1)

    async def _call(
        self, model: str, messages: list[dict[str, Any]], temperature: float
    ) -> LLMResult:
        payload = {"model": model, "messages": messages, "temperature": temperature}
        started = time.perf_counter()
        response = await self._post(payload)
        latency = time.perf_counter() - started  # с повторами: столько ждал ребёнок
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise GatewayError(f"шлюз ответил без ответа модели (model={model})", 200)
        usage = data.get("usage") or {}
        return LLMResult(
            content=choices[0].get("message", {}).get("content") or "",
            model=model,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            latency_s=latency,
            cost=_cost(response),
        )

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        pause = FIRST_PAUSE_S
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                async with self._semaphore:
                    response = await self._http.post("chat/completions", json=payload)
            except httpx.HTTPError as exc:
                if last:
                    raise GatewayError(f"нет связи со шлюзом: {type(exc).__name__}") from exc
                logger.warning("шлюз: %s, повтор через %.0f с", type(exc).__name__, pause)
            else:
                if response.status_code == 200:
                    return response
                if last or response.status_code not in RETRY_STATUSES:
                    raise GatewayError(self._reason(response), response.status_code)
                logger.warning("шлюз: HTTP %d, повтор через %.0f с", response.status_code, pause)
            await self._sleep(pause)
            pause *= 2
        raise AssertionError("unreachable")

    def _reason(self, response: httpx.Response) -> str:
        """Причина отказа из ответа шлюза, без ключа: текст ошибки идёт в лог."""
        try:
            error = response.json().get("error")
            text = error.get("message") if isinstance(error, dict) else error
        except (ValueError, AttributeError):
            text = response.text
        text = str(text or "").replace(self._api_key, "***")[:ERROR_TEXT_LIMIT]
        return f"шлюз ответил HTTP {response.status_code}: {text}"


def _cost(response: httpx.Response) -> float | None:
    raw = response.headers.get(COST_HEADER)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _mime_type(filename: str) -> str:
    return "image/png" if filename.lower().endswith(".png") else "image/jpeg"
