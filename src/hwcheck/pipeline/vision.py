"""Vision-шаг: распознавание страницы с подбором ориентации.

На повёрнутом фото модель возвращает page_ok=false или 0 заданий («неразборчивый
почерк»). Пробуем ориентации по очереди и останавливаемся на первой, где модель
нашла задания. Лишние vision-вызовы тратятся только на проблемных фото.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from hwcheck.llm.base import (
    ChatMessage,
    LLMClient,
    StructuredOutputError,
    VisionClient,
    chat_structured,
    extract_json,
)
from hwcheck.pipeline.normalize import normalize_image, rotate_image
from hwcheck.pipeline.schemas import VisionPage
from hwcheck.prompts import load_prompt

# 270° (поворот кадра влево при съёмке) у телефонов чаще, чем 90°
ORIENTATIONS = (0, 270, 90, 180)


@dataclass
class RecognizedPage:
    page: VisionPage | None  # None — ни одна ориентация не дала валидный JSON
    orientation: int  # градусы, на которые пришлось довернуть фото
    attempts: int
    tokens_in: int
    tokens_out: int
    latency_s: float
    raw: str  # ответ модели в выбранной (или последней) ориентации


async def recognize_page(
    client: VisionClient,
    image: bytes,
    *,
    prompt: str,
    model: str,
) -> RecognizedPage:
    normalized = normalize_image(image)
    tokens_in = tokens_out = 0
    latency = 0.0
    fallback: tuple[VisionPage, int, str] | None = None
    last_raw = ""

    for attempts, degrees in enumerate(ORIENTATIONS, start=1):
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        # после нормализации байты всегда JPEG — исходное имя (.png и т.п.)
        # дало бы неверный Content-Type при загрузке
        result = await client.analyze_image(data, prompt=prompt, model=model, filename="page.jpg")
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out
        latency += result.latency_s
        last_raw = result.content

        page = _try_parse(result.content)
        if page is not None and page.tasks:
            return RecognizedPage(
                page, degrees, attempts, tokens_in, tokens_out, latency, result.content
            )
        if fallback is None and page is not None:
            # первый валидный (пусть и пустой) ответ — лучший кандидат, если задания не найдутся
            fallback = (page, degrees, result.content)

    if fallback is not None:
        page, degrees, raw = fallback
        return RecognizedPage(page, degrees, len(ORIENTATIONS), tokens_in, tokens_out, latency, raw)
    return RecognizedPage(None, 0, len(ORIENTATIONS), tokens_in, tokens_out, latency, last_raw)


def _try_parse(content: str) -> VisionPage | None:
    try:
        return VisionPage.model_validate_json(extract_json(content))
    except ValidationError:
        return None


# --- Двухэтапное распознавание: транскрипция → структурирование текстом ---
# Vision в JSON-режиме массово отказывает на рукописи («неразборчивый почерк»),
# а свободную транскрипцию той же страницы выдаёт. Структурирует транскрипцию
# уже текстовая модель — надёжный chat_structured, без vision-бюджета.

_DIGITS = re.compile(r"\d")
# «цифра-оператор-цифра» — заголовок «стр. 5 № 4» или мусор из повёрнутых глифов
# набирает цифры, но не имеет формы вычисления
_EQUATION_SHAPE = re.compile(r"\d\s*[=+\-*:×·]\s*\d")

MIN_DIGITS = 3
# ниже — считаем ориентацию сомнительной и продолжаем лестницу
CONF_ACCEPT = 0.5


class VisionAndChatClient(VisionClient, LLMClient, Protocol):
    """Клиент с vision и текстовым chat (GigaChatClient реализует оба)."""


def looks_like_math(transcript: str) -> bool:
    """Дешёвая проверка транскрипции до траты вызова структуризации."""
    return len(_DIGITS.findall(transcript)) >= MIN_DIGITS and bool(
        _EQUATION_SHAPE.search(transcript)
    )


async def recognize_page_two_stage(
    client: VisionAndChatClient,
    image: bytes,
    *,
    vision_model: str,
    structure_model: str,
    transcribe_version: str = "v3",
    structure_version: str = "v1",
) -> RecognizedPage:
    transcribe_prompt = load_prompt("vision", transcribe_version)
    structure_prompt = load_prompt("vision_structure", structure_version)
    normalized = normalize_image(image)
    tokens_in = tokens_out = 0
    latency = 0.0
    last_raw = ""
    # лучший неидеальный кандидат: страница с заданиями низкой уверенности,
    # иначе валидная пустая (сохраняет page_comment — диагноз модели)
    best: tuple[VisionPage, int, str] | None = None

    for attempts, degrees in enumerate(ORIENTATIONS, start=1):
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        result = await client.analyze_image(
            data, prompt=transcribe_prompt, model=vision_model, filename="page.jpg"
        )
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out
        latency += result.latency_s
        last_raw = result.content
        if not looks_like_math(result.content):
            continue

        messages = [
            ChatMessage(role="system", content=structure_prompt),
            ChatMessage(role="user", content=result.content),
        ]
        try:
            page, structure_result = await chat_structured(
                client, messages, VisionPage, model=structure_model
            )
        except StructuredOutputError as exc:
            if exc.result is not None:  # расход неудачных попыток тоже считаем
                tokens_in += exc.result.tokens_in
                tokens_out += exc.result.tokens_out
                latency += exc.result.latency_s
            continue
        tokens_in += structure_result.tokens_in
        tokens_out += structure_result.tokens_out
        latency += structure_result.latency_s

        if page.tasks and min(t.confidence for t in page.tasks) >= CONF_ACCEPT:
            return RecognizedPage(
                page, degrees, attempts, tokens_in, tokens_out, latency, result.content
            )
        # низкая уверенность — возможно, мусор не той ориентации: пробуем дальше,
        # но запоминаем как кандидата
        if best is None or (page.tasks and not best[0].tasks):
            best = (page, degrees, result.content)

    if best is not None:
        page, degrees, raw = best
        return RecognizedPage(page, degrees, len(ORIENTATIONS), tokens_in, tokens_out, latency, raw)
    return RecognizedPage(None, 0, len(ORIENTATIONS), tokens_in, tokens_out, latency, last_raw)
