"""Клиент стенда поверх GigaChat: кэш ответов, повтор при 429, лимит вызовов.

PERS-тариф даёт один одновременный запрос, и тот же ключ использует бот: стенд ходит строго
последовательно, при 429 ждёт и повторяет, повторный прогон берёт ответы из кэша без токенов.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.pipeline.vision import VisionAndChatClient

logger = logging.getLogger(__name__)

RETRY_DELAYS_S = (20.0, 40.0, 60.0)


class BudgetExceeded(RuntimeError):
    """Прогон израсходовал лимит свежих вызовов модели."""


@dataclass
class ClientStats:
    fresh_calls: int = 0
    cached_calls: int = 0
    tokens: int = 0  # включая исходные токены закэшированных ответов — стоимость конфигурации
    rate_limited: int = 0

    def snapshot(self) -> "ClientStats":
        return ClientStats(self.fresh_calls, self.cached_calls, self.tokens, self.rate_limited)


class BenchClient:
    def __init__(
        self,
        inner: VisionAndChatClient,
        cache_dir: Path,
        *,
        max_calls: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self._max_calls = max_calls
        self._sleep = sleep
        self.stats = ClientStats()

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        key = _key(
            {
                "kind": "chat",
                "model": model,
                "temperature": temperature,
                "messages": [m.model_dump() for m in messages],
            }
        )
        return await self._cached(
            key, lambda: self._inner.chat(messages, model=model, temperature=temperature)
        )

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        key = _key(
            {
                "kind": "image",
                "model": model,
                "prompt": prompt,
                "image": hashlib.sha256(image).hexdigest(),
            }
        )
        return await self._cached(
            key,
            lambda: self._inner.analyze_image(image, prompt=prompt, model=model, filename=filename),
        )

    async def _cached(self, key: str, call: Callable[[], Awaitable[LLMResult]]) -> LLMResult:
        path = self._cache_dir / f"{key}.json"
        if path.exists():
            result = LLMResult.model_validate_json(path.read_text(encoding="utf-8"))
            self.stats.cached_calls += 1
            self.stats.tokens += result.tokens_in + result.tokens_out
            return result
        if self._max_calls is not None and self.stats.fresh_calls >= self._max_calls:
            raise BudgetExceeded(f"лимит {self._max_calls} свежих вызовов исчерпан")
        result = await self._with_retry(call)
        self.stats.fresh_calls += 1
        self.stats.tokens += result.tokens_in + result.tokens_out
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(result.model_dump_json(), encoding="utf-8")
        tmp.replace(path)
        return result

    async def _with_retry(self, call: Callable[[], Awaitable[LLMResult]]) -> LLMResult:
        for delay in (*RETRY_DELAYS_S, None):
            try:
                return await call()
            except Exception as exc:
                if delay is None or not _rate_limited(exc):
                    raise
                self.stats.rate_limited += 1
                logger.warning("GigaChat 429, пауза %.0f с", delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")


def _rate_limited(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc)


def _key(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
