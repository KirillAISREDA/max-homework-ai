"""Нормализация фото перед отправкой в vision.

Фото из мессенджеров приходят без EXIF и в произвольной ориентации (телефон
снимает боком, метаданные вырезаются). Без EXIF ориентацию рукописи надёжно
не определить — её подбирает retry-лестница в pipeline.vision по результату
распознавания. Здесь — детерминированная часть: EXIF-поворот, ресайз, JPEG.
"""

import io

from PIL import Image, ImageOps

MAX_SIDE = 2048
JPEG_QUALITY = 90


def normalize_image(data: bytes) -> bytes:
    image = Image.open(io.BytesIO(data))
    transposed = ImageOps.exif_transpose(image)
    if max(transposed.size) > MAX_SIDE:
        transposed.thumbnail((MAX_SIDE, MAX_SIDE))
    return _to_jpeg(transposed)


def rotate_image(data: bytes, degrees: int) -> bytes:
    """Поворот по часовой стрелке на 90/180/270 градусов."""
    image = Image.open(io.BytesIO(data))
    return _to_jpeg(image.rotate(-degrees, expand=True))


def _to_jpeg(image: Image.Image) -> bytes:
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
