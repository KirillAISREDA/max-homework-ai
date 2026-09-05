"""Модели обновлений MAX Bot API (dev.max.ru/docs-api).

Парсинг толерантный (extra=ignore, всё опционально): формат апдейтов проверим
живьём при первом запуске, незнакомые поля не должны ронять бота.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MaxUser(_Model):
    user_id: int | None = None
    name: str | None = None


class MaxRecipient(_Model):
    chat_id: int | None = None
    user_id: int | None = None


class MaxAttachment(_Model):
    type: str = ""
    payload: dict[str, Any] = {}

    @property
    def url(self) -> str | None:
        value = self.payload.get("url")
        return value if isinstance(value, str) else None


class MaxMessageBody(_Model):
    mid: str | None = None
    text: str | None = None
    attachments: list[MaxAttachment] = []


class MaxLink(_Model):
    """Пересланное (forward) или отвеченное (reply) сообщение.

    При пересылке фото боту body внешнего сообщения приходит пустым — вложения
    лежат здесь (живой тест, сессия 9)."""

    type: str = ""
    message: MaxMessageBody | None = None


class MaxMessage(_Model):
    sender: MaxUser | None = None
    recipient: MaxRecipient | None = None
    timestamp: int | None = None
    body: MaxMessageBody | None = None
    link: MaxLink | None = None

    @property
    def chat_id(self) -> int | None:
        return self.recipient.chat_id if self.recipient else None

    @property
    def image_urls(self) -> list[str]:
        """Свои вложения первыми, затем из пересланного/отвеченного сообщения."""
        bodies = [self.body, self.link.message if self.link else None]
        return [
            a.url
            for body in bodies
            if body is not None
            for a in body.attachments
            if a.type == "image" and a.url
        ]


class MaxCallback(_Model):
    callback_id: str | None = None
    payload: str | None = None
    user: MaxUser | None = None


class MaxUpdate(_Model):
    update_type: str = ""
    timestamp: int | None = None
    message: MaxMessage | None = None
    callback: MaxCallback | None = None
    chat_id: int | None = None
    user: MaxUser | None = None

    @property
    def effective_chat_id(self) -> int | None:
        if self.message is not None and self.message.chat_id is not None:
            return self.message.chat_id
        return self.chat_id

    @property
    def effective_user_id(self) -> int | None:
        if self.callback is not None and self.callback.user is not None:
            return self.callback.user.user_id
        if self.message is not None and self.message.sender is not None:
            return self.message.sender.user_id
        return self.user.user_id if self.user else None
