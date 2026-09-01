"""Клиент MAX Bot API (арх. §3.1): единственное место, знающее формат MAX.

Авторизация — токен бота в заголовке Authorization. Long polling (GET /updates,
marker) — для разработки; webhook (POST /subscriptions) добавим при деплое.
"""

import logging
from types import TracebackType
from typing import Any, Self

import httpx

from hwcheck.bot.models import MaxUpdate

logger = logging.getLogger(__name__)

Buttons = list[list[dict[str, str]]]


class MaxClient:
    def __init__(self, token: str, base_url: str = "https://platform-api2.max.ru") -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": token},
            timeout=httpx.Timeout(100.0),  # long polling до 90 с
        )
        # для скачивания медиа по абсолютным URL из апдейтов: токен бота
        # не должен уходить на сторонний (CDN-) хост
        self._files = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()
        await self._files.aclose()

    async def get_updates(
        self, marker: int | None, *, timeout: int = 30
    ) -> tuple[list[MaxUpdate], int | None]:
        params: dict[str, Any] = {"timeout": timeout, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        response = await self._http.get("/updates", params=params)
        response.raise_for_status()
        data = response.json()
        updates = [MaxUpdate.model_validate(u) for u in data.get("updates", [])]
        return updates, data.get("marker")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Buttons | None = None,
    ) -> None:
        body: dict[str, Any] = {"text": text}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        response = await self._http.post("/messages", params={"chat_id": chat_id}, json=body)
        response.raise_for_status()

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        response = await self._http.post("/answers", params={"callback_id": callback_id}, json=body)
        response.raise_for_status()

    async def download(self, url: str) -> bytes:
        """Скачивает вложение (фото) по URL из апдейта — без токена бота."""
        response = await self._files.get(url)
        response.raise_for_status()
        return response.content

    async def me(self) -> dict[str, Any]:
        response = await self._http.get("/me")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data


def callback_button(text: str, payload: str) -> dict[str, str]:
    return {"type": "callback", "text": text, "payload": payload}
