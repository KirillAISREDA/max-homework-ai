"""Временное состояние онбординга (спецификация §4, §4.7): Redis, TTL 24 ч, ключ — хэш пользователя.

Здесь то, чему не место в PostgreSQL: ссылка ребёнка, открытая родителем, до «Согласен»/«Отказать»;
фото родителя до ответа «Чья домашка?»; выбранный ребёнок на время одной домашки.
"""

import logging
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

ONBOARDING_TTL_S = 24 * 3600


class OnboardingState(BaseModel):
    pending_invite: str | None = None  # token_hash ссылки ученика, открытой родителем
    pending_photos: list[str] = Field(default_factory=list)  # фото родителя до «Чья домашка?»
    pending_at: float | None = None  # когда пришло первое из этих фото
    child_id: int | None = None  # ребёнок 1–4 класса, выбранный для текущей домашки
    child_chosen_at: float | None = None


class OnboardingStateStore(Protocol):
    async def get(self, user_hash: str) -> OnboardingState: ...

    async def set(self, user_hash: str, state: OnboardingState) -> None: ...


class InMemoryOnboardingStateStore:
    def __init__(self) -> None:
        self._states: dict[str, OnboardingState] = {}

    async def get(self, user_hash: str) -> OnboardingState:
        return self._states.get(user_hash, OnboardingState())

    async def set(self, user_hash: str, state: OnboardingState) -> None:
        self._states[user_hash] = state


class RedisOnboardingStateStore:
    """JSON в `onb:<хэш пользователя>` с TTL, продлевается при записи; сырой id MAX не хранится."""

    def __init__(self, client: Redis, *, ttl_s: int = ONBOARDING_TTL_S) -> None:
        self._client = client
        self._ttl_s = ttl_s

    async def get(self, user_hash: str) -> OnboardingState:
        raw = await self._client.get(f"onb:{user_hash}")
        if raw is None:
            return OnboardingState()
        try:
            return OnboardingState.model_validate_json(raw)
        except ValidationError:
            # схема поменялась между деплоями: пользователь откроет ссылку ещё раз
            logger.warning("onboarding state unreadable, reset")
            return OnboardingState()

    async def set(self, user_hash: str, state: OnboardingState) -> None:
        await self._client.set(f"onb:{user_hash}", state.model_dump_json(), ex=self._ttl_s)
