"""Клиент OCR-сервиса (спецификация каркаса §8): слова «как написано» с координатами.

Сервис недоступен или медленен — OcrError; предметный модуль переводит это в «не уверен» и
событие ocr_failed, проверка не падает.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from hwcheck.subjects.base import Box, Word

OcrWord = Word
# ответ сервиса — это слова одной страницы: больше нескольких мегабайт означает битый или
# враждебный сервис, и разбирать такой JSON (память бота) уже не стоит
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class OcrError(Exception):
    pass


class OcrClient:
    def __init__(self, base_url: str, *, timeout_s: float) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def recognize(self, image: bytes) -> list[Word]:
        # весь разбор ответа — под тем же перехватом: битый ответ (не JSON-объект, words не
        # список, слово без нужных полей) для предметного модуля не отличается от сбоя сети
        try:
            response = await self._http.post(
                "/recognize", content=image, headers={"Content-Type": "image/jpeg"}
            )
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise OcrError("ocr: ответ слишком большой")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("ocr: ответ не объект")
            words = data.get("words", [])
            if not isinstance(words, list):
                raise ValueError("ocr: words не список")
            return [_word(w) for w in words]
        except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError) as exc:
            raise OcrError(f"ocr: {type(exc).__name__}") from exc

    async def health(self) -> bool:
        try:
            response = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _word(raw: Any) -> Word:
    if not isinstance(raw, dict):
        raise ValueError("ocr: слово не объект")
    box = raw.get("box")
    return Word(
        text=str(raw.get("text", "")),
        box=Box(x0=box[0], y0=box[1], x1=box[2], y1=box[3]) if box else None,
        confidence=raw.get("confidence"),
        line=raw.get("line"),
    )
