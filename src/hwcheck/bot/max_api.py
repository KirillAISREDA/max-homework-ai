"""Клиент MAX Bot API (арх. §3.1): единственное место, знающее формат MAX.

Авторизация — токен бота в заголовке Authorization. Long polling (GET /updates,
marker) — для разработки; webhook (POST /subscriptions) добавим при деплое.
"""

import logging
import ssl
from types import TracebackType
from typing import Any, Self

import certifi
import httpx

from hwcheck.bot.models import MaxUpdate

logger = logging.getLogger(__name__)

Buttons = list[list[dict[str, str]]]


def ssl_verify(extra_ca: str | None) -> ssl.SSLContext | bool:
    """Контекст TLS для httpx: корни certifi плюс дополнительный CA.

    platform-api2.max.ru подписан НУЦ Минцифры, которого нет в certifi; медиа же
    могут отдаваться с хостов под публичными CA — поэтому корень добавляется, а не
    подменяет хранилище. Без extra_ca — дефолт httpx (True). Отсутствующий файл —
    FileNotFoundError сразу, а не туманная ошибка TLS на первом запросе.
    """
    if extra_ca is None:
        return True
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=extra_ca)
    return context


class MaxClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api2.max.ru",
        *,
        ca_bundle: str | None = None,
    ) -> None:
        verify = ssl_verify(ca_bundle)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": token},
            timeout=httpx.Timeout(100.0),  # long polling до 90 с
            verify=verify,
        )
        # для скачивания медиа по абсолютным URL из апдейтов: токен бота
        # не должен уходить на сторонний (CDN-) хост
        self._files = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0), follow_redirects=True, verify=verify
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
