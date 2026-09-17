"""Контракт предметного модуля (спецификация каркаса §3–4).

Четыре шага: распознавание → эталон → проверка → тьютор. Общие типы для бота: страница с заданиями,
эталон с происхождением и доверием, находка с силой вердикта. Сила вердикта — по способу проверки:
`verified` только при детерминированном сравнении с проверенным эталоном.
"""

from __future__ import annotations

from secrets import token_hex
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
    # координаты приходят от OCR и из состояния чата: отрицательных пикселей не бывает
    x0: int = Field(ge=0)
    y0: int = Field(ge=0)
    x1: int = Field(ge=0)
    y1: int = Field(ge=0)


class Word(BaseModel):
    """Слово «как написано» с координатами на фото — для кропа в уточняющем вопросе."""

    text: str
    box: Box | None = None
    confidence: float | None = None
    line: int | None = None
    # номер фото в альбоме (`ChatState.photo_paths`), к которому относятся координаты box;
    # `recognize(image)` видит один снимок и не знает его места в альбоме — проставляет бот при
    # сборке альбома (этап 3, когда бот перейдёт на `SubjectPage`), пока всегда 0
    photo_index: int = Field(default=0, ge=0)  # отрицательный индекс брал бы последнее фото


class SubjectTask(BaseModel):
    number: str
    number_on_page: bool = True  # номер написан на странице, а не присвоен распознаванием
    condition: str = ""  # условие задания (с учебника или переписанное учеником)
    lines: list[str] = Field(default_factory=list)  # решение/текст ученика по строкам
    answer: str | None = None
    words: list[Word] = Field(default_factory=list)  # для языков: слова с координатами
    confidence: float = 1.0
    # путь фото учебника в var/kb_photos (проставляет бот): страница сохраняется в базе знаний
    photo_path: str | None = None


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
    # OCR-сервис недоступен/упал: бот пишет событие ocr_failed, проверка не падает (спецификация §8)
    failure: Literal["ocr_failed"] | None = None


class Reference(BaseModel):
    task_number: str
    origin: Origin
    trust: Trust
    payload: dict[str, Any] = Field(default_factory=dict)  # предметное содержимое эталона


class Finding(BaseModel):
    # ссылка на находку переживает пересчёт: позиция в списке находок задания не устойчива
    # (`clarify._merge_findings` заменяет математические находки), а `id` — да
    id: str = Field(default_factory=lambda: token_hex(4))
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
    """Результат проверки одного задания.

    `payload` — предметное содержимое, которое читает только тот, кто его положил, с одним
    исключением на время перехода: у математики бот читает `grade` (`GradeResult` целиком),
    `ref_status`, `solver_from_cache` и `solver_tokens` — сводка и события сейчас строятся
    из `GradeResult`, а не из находок. Для остальных предметов бот читает только `findings`
    и `reference`; математика переходит на тот же путь в этапе 3 (решение R7: `grade`
    становится необязательным, запасной вывод находок — только когда он есть).
    """

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
