"""Vision-шаг: распознавание страницы с подбором ориентации.

На повёрнутом фото модель возвращает page_ok=false или 0 заданий («неразборчивый
почерк»). Пробуем ориентации по очереди и останавливаемся на первой, где модель
нашла задания. Лишние vision-вызовы тратятся только на проблемных фото.
"""

from dataclasses import dataclass

from pydantic import ValidationError

from hwcheck.llm.base import VisionClient, extract_json
from hwcheck.pipeline.normalize import normalize_image, rotate_image
from hwcheck.pipeline.schemas import VisionPage

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
    filename: str = "image.jpg",
) -> RecognizedPage:
    normalized = normalize_image(image)
    tokens_in = tokens_out = 0
    latency = 0.0
    fallback: tuple[VisionPage, int, str] | None = None
    last_raw = ""

    for attempts, degrees in enumerate(ORIENTATIONS, start=1):
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        result = await client.analyze_image(data, prompt=prompt, model=model, filename=filename)
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
