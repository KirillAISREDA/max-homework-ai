import io

from PIL import Image

from hwcheck.pipeline.normalize import MAX_SIDE, normalize_image, rotate_image


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
