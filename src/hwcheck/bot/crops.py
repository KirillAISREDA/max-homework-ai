"""Кроп слова с фото тетради для уточняющего вопроса: ребёнок видит, о чём спрашивают."""

from __future__ import annotations

import io

from PIL import Image

from hwcheck.subjects.base import Box


def crop_word(image: bytes, box: Box, *, margin: int = 12) -> bytes:
    with Image.open(io.BytesIO(image)) as source:
        width, height = source.size
        area = (
            max(0, box.x0 - margin),
            max(0, box.y0 - margin),
            min(width, box.x1 + margin),
            min(height, box.y1 + margin),
        )
        crop = source.convert("RGB").crop(area)
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=90)
        return out.getvalue()
