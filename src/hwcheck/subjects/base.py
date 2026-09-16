"""Контракт предметного модуля (спецификация каркаса §3–4).

Четыре шага: распознавание → эталон → проверка → тьютор. Общие типы для бота: страница с заданиями,
эталон с происхождением и доверием, находка с силой вердикта. Сила вердикта — по способу проверки:
`verified` только при детерминированном сравнении с проверенным эталоном.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from hwcheck.pipeline.tutor import TutorSession
from hwcheck.subjects.kb_models import KnowledgeBase

__all__ = [
    "Box",
    "Finding",
    "KnowledgeBase",
    "Origin",
    "PageRole",
    "Reference",
    "Strength",
    "SubjectModule",
    "SubjectPage",
    "SubjectTask",
    "TaskResult",
    "TaskStrength",
    "Trust",
    "Usage",
    "Word",
    "strength_of_task",
]

Strength = Literal["verified", "candidate", "feedback"]
Trust = Literal["verified", "unverified"]
Origin = Literal["photo", "kb", "derived"]
PageRole = Literal["textbook", "notebook", "unknown"]
TaskStrength = Literal["ok", "verified", "candidate", "feedback"]


class Box(BaseModel):
    x0: int
    y0: int
    x1: int
    y1: int


class Word(BaseModel):
    """Слово «как написано» с координатами на фото — для кропа в уточняющем вопросе."""

    text: str
    box: Box | None = None
    confidence: float | None = None
    line: int | None = None
    # номер фото в альбоме (`ChatState.photo_paths`), к которому относятся координаты box;
    # проставляет предметный модуль в `recognize`
    photo_index: int = 0


class SubjectTask(BaseModel):
    number: str
    number_on_page: bool = True  # номер написан на странице, а не присвоен распознаванием
    condition: str = ""  # условие задания (с учебника или переписанное учеником)
    lines: list[str] = Field(default_factory=list)  # решение/текст ученика по строкам
    answer: str | None = None
    words: list[Word] = Field(default_factory=list)  # для языков: слова с координатами
    confidence: float = 1.0


class Usage(BaseModel):
    calls: int = 0
    tokens: int = 0


class SubjectPage(BaseModel):
    subject: str
    role: PageRole
    tasks: list[SubjectTask]
    comment: str | None = None  # почему страница непригодна
    transcript: str | None = None  # сырая транскрипция — только для dev-логов и стенда
    usage: Usage = Field(default_factory=Usage)


class Reference(BaseModel):
    task_number: str
    origin: Origin
    trust: Trust
    payload: dict[str, Any] = Field(default_factory=dict)  # предметное содержимое эталона


class Finding(BaseModel):
    task_index: int
    kind: str  # arithmetic, spelling, verb_form, missing_word, …
    strength: Strength
    expected: str | None = None
    actual: str | None = None
    line: int | None = None  # 1-based строка решения
    word: Word | None = None
    detail: str | None = None  # текст для сводки: причина «не уверен», описание
    rule_code: str | None = None  # карточка правила из базы знаний
    confirmed: bool | None = None  # ответ ученика на «здесь написано …?»
    resolved: bool = False  # разобрано с тьютором

    @property
    def is_error(self) -> bool:
        return self.strength == "verified" or (
            self.strength == "candidate" and self.confirmed is True
        )


class TaskResult(BaseModel):
    task_index: int
    findings: list[Finding]
    reference: Reference | None = None
    payload: dict[str, Any] = Field(default_factory=dict)  # для тьютора: эталон, разбор строк


def strength_of_task(findings: list[Finding]) -> TaskStrength:
    """Худшая находка задания; отклонённые учеником кандидаты не считаются."""
    live = [f for f in findings if not (f.strength == "candidate" and f.confirmed is False)]
    if any(f.is_error for f in live):
        return "verified"
    if any(f.strength == "candidate" for f in live):
        return "candidate"
    if any(f.strength == "feedback" for f in live):
        return "feedback"
    return "ok"


class SubjectModule(Protocol):
    code: str

    async def recognize(self, image: bytes) -> SubjectPage: ...

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]: ...

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]: ...

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession: ...
