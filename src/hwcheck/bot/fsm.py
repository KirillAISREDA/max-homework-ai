"""Состояние диалога (арх. §3.2): детерминированный FSM, LLM его не контролирует.

idle → checking (фото в обработке) → [clarifying (уточняющие вопросы ученику)] → review
(результаты + кнопки «Разобрать») → tutoring (диалог по одному заданию) → review → …
Хранилище за протоколом:
Redis (арх. §6.2, TTL 24 ч) на сервере, in-memory — локально и в тестах.
"""

import logging
from secrets import token_hex
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

from hwcheck.bot.check import RefStatus
from hwcheck.events import anonymize
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession
from hwcheck.subjects.base import Finding

logger = logging.getLogger(__name__)

STATE_TTL_S = 24 * 3600

DialogPhase = Literal["idle", "checking", "clarifying", "review", "tutoring"]


class CheckedTask(BaseModel):
    task: VisionTask
    ref: RefSolution | None  # None — условия нет, проверка только пересчётом
    grade: GradeResult
    # находки предметного модуля (спецификация каркаса §4); пусто — вывести из grade (математика)
    findings: list[Finding] = Field(default_factory=list)
    # статус эталона (bot/check.py): по умолчанию — старые записи Redis без этого поля
    ref_status: RefStatus = "no_condition"


class Clarification(BaseModel):
    """Вопрос ученику по спорному заданию (bot/clarify.py)."""

    task_index: int
    kind: Literal["answer", "sign", "line", "word"]
    line_index: int | None = None  # строка решения для sign/line
    finding_index: int | None = None  # находка item.findings для word
    attempts: int = 0  # неразобранных ответов
    # метка вопроса в payload кнопок: старая кнопка не должна ответить на следующий вопрос
    token: str = Field(default_factory=lambda: token_hex(4))


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
    clarifications: list[Clarification] = Field(default_factory=list)  # очередь вопросов
    # относительные пути PhotoStore фото альбома по порядку (Word.photo_index — индекс сюда)
    photo_paths: list[str] = Field(default_factory=list)


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
