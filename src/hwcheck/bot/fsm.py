"""Состояние диалога (арх. §3.2): детерминированный FSM, LLM его не контролирует.

idle → checking (фото в обработке) → review (результаты + кнопки «Разобрать»)
→ tutoring (диалог по одному заданию) → review → … Хранилище за протоколом:
in-memory сейчас, Redis (TTL 24 ч) при деплое без смены кода обработчиков.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession

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
