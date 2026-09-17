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
        image_token: str | None = None,
    ) -> None:
        await self._post_message({"chat_id": chat_id}, text, buttons, image_token)

    async def send_to_user(
        self,
        user_id: int,
        text: str,
        *,
        buttons: Buttons | None = None,
    ) -> None:
        """Сообщение пользователю по id MAX, а не в чат, где идёт диалог: второй стороне связки
        «ребёнок ↔ родитель» (онбординг §4.2)."""
        await self._post_message({"user_id": user_id}, text, buttons)

    async def _post_message(
        self,
        params: dict[str, int],
        text: str,
        buttons: Buttons | None,
        image_token: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"text": text}
        attachments: list[dict[str, Any]] = []
        if image_token:
            attachments.append({"type": "image", "payload": {"token": image_token}})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
        if attachments:
            body["attachments"] = attachments
        response = await self._http.post("/messages", params=params, json=body)
        response.raise_for_status()

    async def upload_image(self, image: bytes) -> str:
        """Токен вложения: POST /uploads?type=image даёт адрес загрузки, файл уходит туда
        multipart-полем data (без токена бота — хост сторонний), в ответ — token (dev.max.ru).

        Адрес приходит из ответа MAX, а уходит по нему фрагмент домашки ребёнка: только https
        и без редиректов — 30x не должен молча переслать файл на другой хост.
        """
        response = await self._http.post("/uploads", params={"type": "image"})
        response.raise_for_status()
        upload_url = httpx.URL(str(response.json()["url"]))
        if upload_url.scheme != "https":
            raise ValueError("MAX /uploads: адрес загрузки не по https")
        uploaded = await self._files.post(
            upload_url,
            files={"data": ("word.jpg", image, "image/jpeg")},
            follow_redirects=False,
        )
        uploaded.raise_for_status()
        return _upload_token(uploaded.json())

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        """Подтверждает нажатие кнопки, иначе она «крутится» у пользователя.

        MAX требует в теле `message` или `notification` — пустое `{}` даёт 400 (живьём,
        17.09); пустая строка снимает индикатор без всплывающего текста. Ошибка ответа не
        критична для сценария — пишем в лог с телом ответа и идём дальше; callback_id в лог
        не попадает.
        """
        body: dict[str, Any] = {"notification": notification or ""}
        response = await self._http.post("/answers", params={"callback_id": callback_id}, json=body)
        if response.is_error:
            logger.warning("answer_callback: HTTP %s %s", response.status_code, response.text[:300])

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


def _upload_token(data: Any) -> str:
    """Токен из ответа хранилища: живьём (17.09) приходит `{"photos": {"<ключ>": {"token": …}}}`,
    документация обещает `{"token": …}` — принимаем оба."""
    if isinstance(data, dict):
        if isinstance(data.get("token"), str):
            return str(data["token"])
        photos = data.get("photos")
        if isinstance(photos, dict):
            for item in photos.values():
                if isinstance(item, dict) and isinstance(item.get("token"), str):
                    return str(item["token"])
    raise ValueError("MAX upload: в ответе нет token")


def callback_button(text: str, payload: str) -> dict[str, str]:
    return {"type": "callback", "text": text, "payload": payload}
