"""Записи базы знаний (спецификация §5). Хранилище — db/kb.py; здесь только формы данных."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

AnswerStatus = Literal["unverified", "verified", "rejected"]


class KbTask(BaseModel):
    id: int | None = None
    number: str | None
    condition: str
    task_kind: str  # fill_letters, expand_brackets, verb_form, choose, math


class KbPage(BaseModel):
    id: int | None = None
    subject: str
    grade: int | None = None
    fingerprint: str
    text: str
    photo_path: str | None = None
    tasks: list[KbTask] = Field(default_factory=list)


class KbAnswer(BaseModel):
    id: int | None = None
    task_id: int
    answer: dict[str, Any]
    derived_by: str  # dictionary | rule | llm:<модель>@<версия промпта>
    checked_by: str | None = None  # dictionary | rule | manual
    status: AnswerStatus = "unverified"
    reviewed_at: datetime | None = None

    @property
    def trust(self) -> Literal["verified", "unverified"]:
        deterministic = self.checked_by in ("dictionary", "rule")
        return "verified" if self.status == "verified" or deterministic else "unverified"


class KbRule(BaseModel):
    code: str
    subject: str
    grade_from: int
    title: str
    statement: str
    example: str
    finding_kinds: list[str]


class KnowledgeBase(Protocol):
    """База знаний (спецификация §5); реализации — db/kb.py и db/kb_memory.py (Task 7)."""

    async def find_page(self, subject: str, text: str) -> KbPage | None: ...

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage: ...

    async def answers_for(self, task_id: int) -> list[KbAnswer]: ...

    async def save_answer(self, answer: KbAnswer) -> KbAnswer: ...

    async def rule(self, code: str) -> KbRule | None: ...

    async def words(self, subject: str, source: str) -> set[str]: ...
