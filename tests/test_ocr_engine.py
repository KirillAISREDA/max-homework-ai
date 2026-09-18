"""Движок ReadingPipeline: чистые части (конвертация предсказаний, декодирование входа)
проверяются без весов и без onnxruntime."""

import io

import pytest
from ocrsvc.engine import MAX_IMAGE_PIXELS, decode_image, words_from_predictions
from PIL import Image


def test_words_from_predictions_keeps_text_class_only() -> None:
    pred = {
        "predictions": [
            {"class_name": "shrinked_text", "text": "машына", "bbox": [1, 2, 30, 20],
             "confidence": 0.71, "line_idx": 0},
            {"class_name": "shrinked_text", "text": "едет", "bbox": [35, 2, 60, 20],
             "confidence": 0.9, "line_idx": 0},
            {"class_name": "pupil_comment", "text": "зачёркнуто", "bbox": [0, 0, 1, 1]},
        ]
    }  # fmt: skip
    assert words_from_predictions(pred) == [
        {"text": "машына", "box": [1, 2, 30, 20], "confidence": 0.71, "line": 0},
        {"text": "едет", "box": [35, 2, 60, 20], "confidence": 0.9, "line": 0},
    ]


def test_words_from_predictions_tolerates_missing_fields() -> None:
    pred = {"predictions": [{"class_name": "shrinked_text", "text": None}]}
    assert words_from_predictions(pred) == [
        {"text": "", "box": None, "confidence": None, "line": None}
    ]


def _jpeg(width: int, height: int, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    image.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def test_decode_image_applies_exif_rotation() -> None:
    # ориентация 6 = поворот на 90°: телефон снял «лёжа», пиксели хранятся повернутыми
    array = decode_image(_jpeg(40, 20, orientation=6))
    assert array.shape[:2] == (40, 20)  # (высота, ширина): после поворота вертикальное
    assert array.shape[2] == 3  # BGR для cv2-пайплайна


def test_decode_image_rejects_huge_input() -> None:
    huge = Image.new("L", (1, 1))
    huge = huge.resize((MAX_IMAGE_PIXELS // 1000 + 1, 1000))  # чуть больше лимита
    out = io.BytesIO()
    huge.save(out, format="PNG")
    with pytest.raises(ValueError, match="слишком большое"):
        decode_image(out.getvalue())
