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
from hwcheck.pipeline.normalize import normalize_image, rotate_image
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import SubjectTask, Usage, Word

# без 180°: печатную страницу вверх ногами модель не прочитает и перевёрнутой — типографику она
# узнаёт по общему виду текста, а не по строкам, доворачивать лишний раз незачем — экономим вызов
ORIENTATIONS = (0, 270, 90)
_NUMBER = re.compile(r"(?:упр\w*\.?|№|n)\s*(\d{1,4})", re.IGNORECASE)
# «голый» номер — только когда строка ЦЕЛИКОМ им и является (иначе дата «17 сентября 2026»,
# страница «стр. 12» и год «в 1945 году» ловились бы как номер упражнения); 3 цифры — не 4, чтобы
# год «2026» отдельной строкой не совпал
_BARE_NUMBER = re.compile(r"^(\d{1,3})\.?$")
FILLED_BY_HAND_COMMENT = "страница уже заполнена от руки — нужна чистая страница учебника"


class RuExercise(BaseModel):
    number: str | None = None
    instruction: str = ""
    text: str


class RuPage(BaseModel):
    role: Literal["textbook", "notebook", "unknown"]
    exercises: list[RuExercise] = Field(default_factory=list)
    comment: str | None = None
    # на странице есть рукописные записи ученика (заполненные пропуски, ответы): печатной
    # страницей учебника такая рабочая тетрадь быть перестаёт — поле последним, чтобы старые
    # ответы модели без него валидировались
    handwritten: bool = False


def _hide_handwriting(page: RuPage) -> RuPage:
    """Vision не должен пересказывать рукопись ученика (исследование 13.09: прячет 78–80 % её
    ошибок) — если модель всё же вернула exercises не для "textbook", подчищаем сами, а не
    полагаемся только на промпт.
    """
    if page.role != "textbook":
        return page.model_copy(update={"exercises": []})
    return page


async def recognize_page(
    client: VisionClient, image: bytes, *, model: str, prompt_version: str = "v1"
) -> tuple[RuPage, Usage]:
    """Роль страницы и печатные упражнения.

    На «unknown» и на "textbook" без упражнений пробуем следующую ориентацию — не как в
    `pipeline/vision.py` (математика): без 180° (см. комментарий у `ORIENTATIONS`) и без выбора
    «лучшей» по числу заданий страницы — только первая, где нашлись упражнения (или "notebook").
    """
    prompt = load_prompt("ru_page", prompt_version)
    normalized = normalize_image(image)
    usage = Usage()
    page = RuPage(role="unknown")
    fallback: RuPage | None = None  # первая распознанная не-"unknown" страница — если так и не
    # найдём упражнений ни в одной ориентации, вернём её, а не "unknown" последней попытки
    for degrees in ORIENTATIONS:
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        result = await client.analyze_image(data, prompt=prompt, model=model, filename="page.jpg")
        usage.calls += 1
        usage.tokens += result.tokens_in + result.tokens_out
        try:
            page = RuPage.model_validate_json(extract_json(result.content))
        except ValidationError:
            page = RuPage(role="unknown", comment="ответ модели не разобран")
        page = _hide_handwriting(page)
        if page.role == "textbook" and page.handwritten:
            # заполненная от руки рабочая тетрадь: vision читает её вместе с рукописью ученика,
            # и эталон совпал бы с тем, что ребёнок написал — проверка всегда говорила бы
            # «верно», а текст и фото ушли бы в базу знаний (финальное ревью 17.09, I5).
            # Доворачивать незачем — страница не станет чистой от поворота
            return RuPage(role="unknown", exercises=[], comment=FILLED_BY_HAND_COMMENT), usage
        if page.role == "notebook" or page.exercises:
            return page, usage
        if fallback is None and page.role != "unknown":
            fallback = page
    return (fallback if fallback is not None else page), usage


def task_kind(condition: str) -> str:
    if "_" in condition:
        return "fill_letters"
    if "(" in condition:
        return "expand_brackets"
    return "copy"


def textbook_tasks(page: RuPage, photo_path: str | None) -> list[SubjectTask]:
    """Номер — печатный, если есть; иначе наименьшее свободное число (не занятое печатными
    номерами этой страницы и уже розданными автономерами — коллизий не бывает, спецификация §6).
    """
    exercises = [exercise for exercise in page.exercises if exercise.text.strip()]
    used = {int(e.number) for e in exercises if e.number is not None and e.number.isdigit()}
    tasks = []
    for exercise in exercises:
        if exercise.number is not None:
            number, number_on_page = exercise.number, True
        else:
            candidate = 1
            while candidate in used:
                candidate += 1
            used.add(candidate)
            number, number_on_page = str(candidate), False
        tasks.append(
            SubjectTask(
                number=number,
                number_on_page=number_on_page,
                condition=exercise.text.strip(),
                photo_path=photo_path,
            )
        )
    return tasks


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
    """Номер упражнения в первых строках тетради: «Упр. 245», «№ 245» — где угодно в строке;
    «голый» номер («245.») — только если им является строка целиком (иначе дата, номер страницы
    учебника и год ловились бы как номер упражнения, ревью 17.09)."""
    for line in lines:
        match = _NUMBER.search(line)
        if match:
            return match.group(1)
    for line in lines:
        match = _BARE_NUMBER.match(line.strip())
        if match:
            return match.group(1)
    return None
