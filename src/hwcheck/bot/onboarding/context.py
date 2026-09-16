"""Общее для шагов онбординга (спецификация §4, §7, §11): кто пишет, чем ответить, где хранить.

Шаги (subject, student, parent, linking) получают один OnboardingContext; часы подменяются в тестах.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hwcheck.bot.fsm import StateStore
from hwcheck.bot.max_api import Buttons, MaxClient
from hwcheck.bot.onboarding.state import OnboardingStateStore
from hwcheck.bot.subjects import school_year
from hwcheck.crypto import UserIdCipher
from hwcheck.db.repo import ProfileRepository
from hwcheck.events import EventLog, anonymize

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))  # учебный год — по московской дате


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Actor:
    """Автор апдейта: чат для ответа, id MAX для журнала и шифра, хэш для поиска в базе."""

    chat_id: int
    user_id: int
    user_hash: str

    @classmethod
    def of(cls, chat_id: int, user_id: int) -> Actor:
        user_hash = anonymize(user_id)
        assert user_hash is not None  # None только для user_id=None
        return cls(chat_id, user_id, user_hash)


@dataclass(frozen=True)
class OnboardingContext:
    max: MaxClient
    repo: ProfileRepository
    states: OnboardingStateStore
    dialogs: StateStore  # состояние проверки (bot/fsm.py): текст родителя в разборе идёт тьютору
    events: EventLog
    cipher: UserIdCipher
    bot_username: str
    clock: Callable[[], datetime] = utc_now

    def now(self) -> datetime:
        return self.clock()

    def school_year(self) -> int:
        return school_year(self.clock().astimezone(MSK).date())

    def encrypted_id(self, actor: Actor) -> bytes:
        return self.cipher.encrypt(actor.user_id)

    async def reply(self, actor: Actor, text: str, buttons: Buttons | None = None) -> None:
        await self.max.send_message(actor.chat_id, text, buttons=buttons)

    def log(self, event: str, actor: Actor, *, user_initiated: bool = True, **fields: Any) -> None:
        self.events.log(event, user_id=actor.user_id, user_initiated=user_initiated, **fields)

    async def notify(
        self,
        actor: Actor,
        user_id_enc: bytes,
        text: str,
        *,
        kind: str,
        buttons: Buttons | None = None,
    ) -> None:
        """Сообщение второй стороне связки. Не дошло (бот заблокирован) — событие notify_failed,
        а не сбой апдейта: у автора действие уже выполнено (§9.4, §11)."""
        try:
            await self.max.send_to_user(self.cipher.decrypt(user_id_enc), text, buttons=buttons)
        except Exception as exc:
            logger.warning("notify failed: %s (%s)", kind, type(exc).__name__)
            self.log(
                "notify_failed",
                actor,
                user_initiated=False,
                component="notifier",
                kind=kind,
                error=type(exc).__name__,
            )
            return
        self.log("notify_sent", actor, user_initiated=False, component="notifier", kind=kind)
