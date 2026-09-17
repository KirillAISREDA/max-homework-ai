"""Распознавание страниц для русского языка (спецификация §6).

Учебник — печатный текст, его читает GigaChat-vision (с пропусками «как напечатано»). Тетрадь —
рукопись ученика, её vision НЕ читает (исследование 13.09: скрывает 78–80 % ошибок), только
определяет роль; слова «как написано» даёт OCR-сервис (`OcrClient`).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from hwcheck.llm.base import VisionClient, extract_json
from hwcheck.pipeline.normalize import ImageDecodeError, normalize_image, rotate_image
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import SubjectTask, Usage, Word

ORIENTATIONS = (0, 270, 90)
_NUMBER = re.compile(r"(?:упр\w*\.?|№|n)\s*(\d{1,4})", re.IGNORECASE)
_TOKEN_NUMBER = re.compile(r"^(\d{1,4})\.?$")


class RuExercise(BaseModel):
    number: str | None = None
    instruction: str = ""
    text: str


class RuPage(BaseModel):
    role: Literal["textbook", "notebook", "unknown"]
    exercises: list[RuExercise] = Field(default_factory=list)
    comment: str | None = None


async def recognize_page(
    client: VisionClient, image: bytes, *, model: str, prompt_version: str = "v1"
) -> tuple[RuPage, Usage]:
    """Роль страницы и печатные упражнения; на «unknown» пробуем повернуть фото (как vision.py)."""
    prompt = load_prompt("ru_page", prompt_version)
    try:
        normalized = normalize_image(image)
    except ImageDecodeError:
        # битое фото — отдаём как есть, пусть об этом скажет модель (page.role останется "unknown")
        normalized = image
    usage = Usage()
    page = RuPage(role="unknown")
    for degrees in ORIENTATIONS:
        data = normalized
        if degrees != 0:
            try:
                data = rotate_image(normalized, degrees)
            except ImageDecodeError:
                data = normalized
        result = await client.analyze_image(data, prompt=prompt, model=model, filename="page.jpg")
        usage.calls += 1
        usage.tokens += result.tokens_in + result.tokens_out
        try:
            page = RuPage.model_validate_json(extract_json(result.content))
        except ValidationError:
            page = RuPage(role="unknown", comment="ответ модели не разобран")
        if page.role != "unknown":
            return page, usage
    return page, usage


def task_kind(condition: str) -> str:
    if "_" in condition:
        return "fill_letters"
    if "(" in condition:
        return "expand_brackets"
    return "copy"


def textbook_tasks(page: RuPage, photo_path: str | None) -> list[SubjectTask]:
    return [
        SubjectTask(
            number=exercise.number or str(index),
            number_on_page=exercise.number is not None,
            condition=exercise.text.strip(),
            photo_path=photo_path,
        )
        for index, exercise in enumerate(page.exercises, start=1)
        if exercise.text.strip()
    ]


def notebook_task(words: list[Word]) -> SubjectTask:
    """Страница тетради — одно задание: строки по `line`, слова слева направо."""
    ordered = sorted(words, key=lambda w: (w.line or 0, w.box.x0 if w.box else 0))
    lines: dict[int, list[str]] = {}
    for word in ordered:
        lines.setdefault(word.line or 0, []).append(word.text)
    texts = [" ".join(parts) for _, parts in sorted(lines.items())]
    number = header_number(texts[:2])
    return SubjectTask(
        number=number or "1",
        number_on_page=number is not None,
        lines=texts,
        words=ordered,
        confidence=min((w.confidence for w in words if w.confidence is not None), default=1.0),
    )


def header_number(lines: list[str]) -> str | None:
    """Номер упражнения в первых строках тетради: «Упр. 245», «№ 245» или токеном «245.»."""
    for line in lines:
        match = _NUMBER.search(line)
        if match:
            return match.group(1)
    for line in lines:
        for token in line.split():
            match = _TOKEN_NUMBER.match(token)
            if match:
                return match.group(1)
    return None
