"""Состояние диалога (арх. §3.2): детерминированный FSM, LLM его не контролирует.

idle → checking (фото в обработке) → review (результаты + кнопки «Разобрать»)
→ tutoring (диалог по одному заданию) → review → … Хранилище за протоколом:
Redis (арх. §6.2, TTL 24 ч) на сервере, in-memory — локально и в тестах.
"""

import logging
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

from hwcheck.events import anonymize
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession

logger = logging.getLogger(__name__)

STATE_TTL_S = 24 * 3600

DialogPhase = Literal["idle", "checking", "review", "tutoring"]


class CheckedTask(BaseModel):
    task: VisionTask
    ref: RefSolution | None  # None — условия нет, проверка только пересчётом
    grade: GradeResult


class ChatState(BaseModel):
    phase: DialogPhase = "idle"
    tasks: list[CheckedTask] = Field(default_factory=list)
    tutor: TutorSession | None = None
    tutoring_index: int | None = None
    resolved_indices: list[int] = Field(default_factory=list)  # разобранные ошибки
    # условия со страниц учебника по номеру задания: фото тетради может прийти
    # следующим сообщением (сценарий «учебник + тетрадь», сессия 9)
    textbook_tasks: list[VisionTask] = Field(default_factory=list)
    textbook_saved_at: float | None = None  # время сохранения условий (TTL в pages.py)


class StateStore(Protocol):
    async def get(self, chat_id: int) -> ChatState: ...

    async def set(self, chat_id: int, state: ChatState) -> None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._states: dict[int, ChatState] = {}

    async def get(self, chat_id: int) -> ChatState:
        return self._states.get(chat_id, ChatState())

    async def set(self, chat_id: int, state: ChatState) -> None:
        self._states[chat_id] = state


class RedisStateStore:
    """Состояние чата — JSON в `fsm:<обезличенный chat_id>` с TTL, продлевается при записи."""

    def __init__(self, client: Redis, *, ttl_s: int = STATE_TTL_S) -> None:
        self._client = client
        self._ttl_s = ttl_s

    async def get(self, chat_id: int) -> ChatState:
        raw = await self._client.get(_key(chat_id))
        if raw is None:
            return ChatState()
        try:
            return ChatState.model_validate_json(raw)
        except ValidationError:
            # схема ChatState поменялась между деплоями: чат начинает заново, а не падает
            logger.warning("chat state unreadable, reset to idle")
            return ChatState()

    async def set(self, chat_id: int, state: ChatState) -> None:
        await self._client.set(_key(chat_id), state.model_dump_json(), ex=self._ttl_s)


def _key(chat_id: int) -> str:
    # 152-ФЗ: сырой id MAX не хранится нигде, включая ключи Redis
    return f"fsm:{anonymize(chat_id)}"
