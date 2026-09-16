"""Набор для сценарных тестов онбординга: фейк MAX, часы, контекст в памяти, апдейты."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.invites import new_invite
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.state import InMemoryOnboardingStateStore
from hwcheck.crypto import UserIdCipher, new_user_id_key
from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.repo import StudentProfile
from hwcheck.events import EventLog

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BOT = "domashka_bot"


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Buttons | None]] = []  # (chat_id, текст, кнопки)
        self.to_users: list[tuple[int, str, Buttons | None]] = []  # (user_id, текст, кнопки)
        self.callbacks: list[str] = []
        self.downloads: list[str] = []
        self.blocked_users: set[int] = set()  # заблокировали бота: send_to_user падает

    async def send_message(
        self, chat_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def send_to_user(
        self, user_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        if user_id in self.blocked_users:
            raise RuntimeError("blocked by user")
        self.to_users.append((user_id, text, buttons))

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        self.callbacks.append(callback_id)

    async def download(self, url: str) -> bytes:
        self.downloads.append(url)
        return b"fake-image"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Kit:
    ctx: OnboardingContext
    max: FakeMax
    repo: InMemoryProfileRepository
    clock: Clock
    events_path: Path

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = [json.loads(line) for line in self.events_path.read_text("utf-8").splitlines()]
        return [row for row in rows if event_type is None or row["type"] == event_type]

    def texts(self, user_id: int) -> list[str]:
        return [text for chat_id, text, _ in self.max.sent if chat_id == chat(user_id)]

    def last(self, user_id: int) -> tuple[str, Buttons | None]:
        _, text, buttons = [m for m in self.max.sent if m[0] == chat(user_id)][-1]
        return text, buttons


def make_kit(tmp_path: Path) -> Kit:
    fake = FakeMax()
    repo = InMemoryProfileRepository()
    clock = Clock()
    events_path = tmp_path / "events.jsonl"
    ctx = OnboardingContext(
        max=fake,  # type: ignore[arg-type]
        repo=repo,
        states=InMemoryOnboardingStateStore(),
        dialogs=InMemoryStateStore(),
        events=EventLog(events_path, "dev"),
        cipher=UserIdCipher(new_user_id_key()),
        bot_username=BOT,
        clock=clock,
    )
    return Kit(ctx, fake, repo, clock, events_path)


def chat(user_id: int) -> int:
    """Чат диалога с ботом отличается от id пользователя, как в MAX."""
    return user_id * 10


def actor(user_id: int) -> Actor:
    return Actor.of(chat(user_id), user_id)


def start(user_id: int, payload: str | None = None) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "bot_started",
            "chat_id": chat(user_id),
            "user": {"user_id": user_id},
            "payload": payload,
        }
    )


def text(user_id: int, body: str) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": {"chat_id": chat(user_id)},
                "body": {"mid": "m", "text": body, "attachments": []},
            },
        }
    )


def recipient(user_id: int, chat_type: str | None = None) -> dict[str, Any]:
    """Получатель сообщения; chat_type не задан — как в диалоге с ботом (поле не пришло)."""
    value: dict[str, Any] = {"chat_id": chat(user_id)}
    if chat_type is not None:
        value["chat_type"] = chat_type
    return value


def photo(user_id: int, *urls: str, chat_type: str | None = None) -> MaxUpdate:
    attachments = [{"type": "image", "payload": {"url": url}} for url in urls]
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": recipient(user_id, chat_type),
                "body": {"mid": "m", "text": None, "attachments": attachments},
            },
        }
    )


def press(user_id: int, payload: str, *, chat_type: str | None = None) -> MaxUpdate:
    """Нажатие кнопки; chat_type задан — с сообщением, к которому кнопка приложена."""
    update: dict[str, Any] = {
        "update_type": "message_callback",
        "chat_id": chat(user_id),
        "callback": {
            "callback_id": f"cb-{payload}",
            "payload": payload,
            "user": {"user_id": user_id},
        },
    }
    if chat_type is not None:
        update["message"] = {"recipient": recipient(user_id, chat_type)}
    return MaxUpdate.model_validate(update)


def payloads(buttons: Buttons | None) -> list[str]:
    return [button["payload"] for row in buttons or [] for button in row]


def link_token(message: str) -> str:
    match = re.search(r"\?start=[pc]_([A-Za-z0-9_-]+)", message)
    assert match is not None, message
    return match.group(1)


def code_of(message: str) -> str:
    """Запасной код из сообщения со ссылкой, как его вводит пользователь: «4F7K-92QD»."""
    match = re.search(r"код ([A-Z0-9]{4}-[A-Z0-9]{4})", message)
    assert match is not None, message
    return match.group(1)


async def ready_student(kit: Kit, user_id: int = 1, grade: int = 7) -> StudentProfile:
    """Ученик с предметом и подключённым родителем (родитель — user_id + 100)."""
    me, parent = actor(user_id), actor(user_id + 100)
    profile = await kit.repo.create_student(
        me.user_hash, kit.ctx.encrypted_id(me), grade, kit.ctx.school_year()
    )
    assert profile is not None and profile.user_id is not None
    await kit.repo.set_subject(profile.id, "math")
    invite = new_invite("student_invites_parent")
    await kit.repo.create_invite(invite, profile.user_id, kit.clock.now + timedelta(days=7))
    outcome = await kit.repo.accept_parent_invite(
        invite.token_hash, parent.user_hash, kit.ctx.encrypted_id(parent), "v0", kit.clock.now
    )
    assert outcome.profile is not None
    return outcome.profile
