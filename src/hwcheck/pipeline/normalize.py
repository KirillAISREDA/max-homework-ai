"""Нормализация фото перед отправкой в vision.

Фото из мессенджеров приходят без EXIF и в произвольной ориентации (телефон
снимает боком, метаданные вырезаются). Без EXIF ориентацию рукописи надёжно
не определить — её подбирает retry-лестница в pipeline.vision по результату
распознавания. Здесь — детерминированная часть: EXIF-поворот, ресайз, JPEG.
"""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 2048
JPEG_QUALITY = 90


class ImageDecodeError(ValueError):
    """Байты не являются пригодным изображением (битый файл, не-картинка, bomb)."""


def normalize_image(data: bytes) -> bytes:
    image = _open(data)
    transposed = ImageOps.exif_transpose(image)
    if max(transposed.size) > MAX_SIDE:
        transposed.thumbnail((MAX_SIDE, MAX_SIDE))
    return _to_jpeg(transposed)


def rotate_image(data: bytes, degrees: int) -> bytes:
    """Поворот по часовой стрелке на 90/180/270 градусов."""
    return _to_jpeg(_open(data).rotate(-degrees, expand=True))


def _open(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDecodeError(f"Не удалось прочитать изображение: {exc}") from exc
    return image


def _to_jpeg(image: Image.Image) -> bytes:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        # convert("RGB") просто отбрасывает альфу (прозрачное → чёрное);
        # рукопись на прозрачном фоне обязана лечь на белый
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
