import io

import pytest
from PIL import Image

from hwcheck.pipeline.normalize import (
    MAX_SIDE,
    ImageDecodeError,
    normalize_image,
    rotate_image,
)


def make_jpeg(width: int, height: int, exif_orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    buffer = io.BytesIO()
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        image.save(buffer, format="JPEG", exif=exif)
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def test_normalize_resizes_large_image() -> None:
    result = normalize_image(make_jpeg(4000, 3000))
    assert max(size_of(result)) == MAX_SIDE


def test_normalize_keeps_small_image_size() -> None:
    result = normalize_image(make_jpeg(1280, 960))
    assert size_of(result) == (1280, 960)


def test_normalize_applies_exif_orientation() -> None:
    # orientation 6 = поворот на 90° по часовой: пиксели лежат 1280x960, показывать надо 960x1280
    result = normalize_image(make_jpeg(1280, 960, exif_orientation=6))
    assert size_of(result) == (960, 1280)


def test_rotate_90_swaps_dimensions() -> None:
    result = rotate_image(make_jpeg(1280, 960), 90)
    assert size_of(result) == (960, 1280)


def test_rotate_180_keeps_dimensions() -> None:
    result = rotate_image(make_jpeg(1280, 960), 180)
    assert size_of(result) == (1280, 960)


def test_transparent_png_composited_on_white() -> None:
    # прозрачные пиксели с чёрным RGB под альфой не должны стать чёрным фоном
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = normalize_image(buffer.getvalue())

    pixel = Image.open(io.BytesIO(result)).getpixel((50, 50))
    assert isinstance(pixel, tuple)
    assert all(channel > 240 for channel in pixel[:3])


def test_png_output_is_jpeg() -> None:
    image = Image.new("RGB", (100, 100), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = normalize_image(buffer.getvalue())

    assert Image.open(io.BytesIO(result)).format == "JPEG"


def test_corrupt_bytes_raise_domain_error() -> None:
    with pytest.raises(ImageDecodeError):
        normalize_image(b"definitely not an image")
