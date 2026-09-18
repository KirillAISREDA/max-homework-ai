"""Кроп слова с фото тетради для уточняющего вопроса: ребёнок видит, о чём спрашивают."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from hwcheck.subjects.base import Box


def crop_word(image: bytes, box: Box, *, margin: int = 12) -> bytes:
    """Координаты `box` — в кадре, развёрнутом по EXIF: так их отдаёт OCR (`ocrsvc/engine.py`
    делает `exif_transpose` перед распознаванием). Кроп разворачивает фото так же, иначе у
    снятого «лёжа» телефоном фото ребёнок видит не то слово, о котором спрашивают (ревью, I3)."""
    with Image.open(io.BytesIO(image)) as opened:
        source = ImageOps.exif_transpose(opened) or opened
        width, height = source.size
        area = (
            max(0, box.x0 - margin),
            max(0, box.y0 - margin),
            min(width, box.x1 + margin),
            min(height, box.y1 + margin),
        )
        # сначала crop, потом convert: RGB-копия делается только с фрагмента, а не со всего фото
        crop = source.crop(area).convert("RGB")
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=90)
        return out.getvalue()
