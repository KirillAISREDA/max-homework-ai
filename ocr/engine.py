"""Движки распознавания рукописи: контракт, фейк и реальный (ReadingPipeline)."""

from __future__ import annotations

import io
import json
import os
from typing import Any, Protocol

import numpy as np

MAX_IMAGE_PIXELS = 30_000_000  # ~ 6000×5000: больше — не фото тетради с телефона
TEXT_CLASS = "shrinked_text"


class Engine(Protocol):
    name: str

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        """Слова «как написано»: {text, box: [x0, y0, x1, y1] | None, confidence, line}."""
        ...


class FakeEngine:
    """Для тестов и первого запуска контейнера: слова из аргумента или OCR_FAKE_WORDS (JSON)."""

    name = "fake"

    def __init__(self, words: list[dict[str, Any]] | None = None) -> None:
        raw = os.environ.get("OCR_FAKE_WORDS")
        self._words = words if words is not None else (json.loads(raw) if raw else [])

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        return list(self._words)


def decode_image(image: bytes) -> np.ndarray[Any, Any]:
    """Байты → BGR-массив для ReadingPipeline (cv2-соглашение).

    EXIF-поворот применяется здесь: телефон хранит пиксели «лёжа» и пишет ориентацию в EXIF,
    cv2.imdecode её игнорирует и сегментация получила бы повёрнутую страницу (бэклог OCR из
    ревью каркаса). Лимит пикселей — до раскодирования: decompression bomb в общем контейнере
    с лимитом 2 ГБ убил бы и текущую проверку.
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image)) as source:
        width, height = source.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"изображение слишком большое: {width}×{height}")
        rotated = ImageOps.exif_transpose(source) or source
        rgb = np.asarray(rotated.convert("RGB"))
    return rgb[:, :, ::-1].copy()  # RGB → BGR


def words_from_predictions(pred: dict[str, Any]) -> list[dict[str, Any]]:
    """Формат ответа сервиса (ocr/README.md) из предсказаний PipelinePredictor."""
    return [
        {
            "text": p.get("text") or "",
            "box": p.get("bbox"),
            "confidence": p.get("confidence"),
            "line": p.get("line_idx"),
        }
        for p in pred.get("predictions", [])
        if p.get("class_name") == TEXT_CLASS
    ]


class ReadingPipelineEngine:
    """ReadingPipeline (ai-forever) на ONNX/CPU без языковой модели и без torch.

    Настройки onnxruntime по спайку памяти 17.09: арена и mem-pattern выключены (пик 2,5 → 1,46 ГБ
    при побайтово том же тексте); сегментация и OCR — родное разрешение весов (896×896 / 64×512),
    понижать нельзя (теряется 10–31 % слов).
    """

    name = "readingpipeline"

    def __init__(self, weights_dir: str, *, threads: int = 2) -> None:
        # onnxruntime/ocrpipeline не установлены в окружении бота (тяжёлые зависимости только в
        # образе ocr/Dockerfile) — mypy.overrides ignore_missing_imports для обоих в pyproject.toml
        import onnxruntime as ort

        arena = os.environ.get("OCR_ORT_ARENA", "0") == "1"
        mem_pattern = os.environ.get("OCR_ORT_MEMPATTERN", "0") == "1"
        original = ort.SessionOptions

        def session_options(*args: Any, **kwargs: Any) -> Any:
            options = original(*args, **kwargs)
            options.enable_cpu_mem_arena = arena
            options.enable_mem_pattern = mem_pattern
            return options

        # апстримный OCR-model/SEGM-model создаёт ort.SessionOptions() сам; подменяем конструктор
        # в общем модуле, не правя клоны (как в спайке)
        ort.SessionOptions = session_options
        os.chdir(weights_dir)  # пути в pipeline_config.json относительные
        config_path = _runtime_config(weights_dir, threads)
        from ocrpipeline.predictor import PipelinePredictor

        self._predictor = PipelinePredictor(pipeline_config_path=config_path)

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        _rotated, pred = self._predictor(decode_image(image))
        return words_from_predictions(pred)


def _runtime_config(weights_dir: str, threads: int) -> str:
    """pipeline_config.json весов настроен на GPU/PyTorch/KenLM; для CPU-ONNX без LM пишем копию."""
    original = os.path.join(weights_dir, "pipeline_config.json")
    runtime = os.path.join(weights_dir, "pipeline_config.runtime.json")
    with open(original, encoding="utf-8") as source:
        config = json.load(source)
    main_process = config["main_process"]
    for step, onnx_path in (
        ("SegmPrediction", "segm/segm_model.onnx"),
        ("OCRPrediction", "ocr/ocr_model.onnx"),
    ):
        main_process[step]["device"] = "cpu"
        main_process[step]["runtime"] = "ONNX"
        main_process[step]["model_path"] = onnx_path
        main_process[step]["num_threads"] = threads
    main_process["OCRPrediction"]["lm_path"] = ""  # LM выключен (исследование 13.09)
    with open(runtime, "w", encoding="utf-8") as out:
        json.dump(config, out, ensure_ascii=False, indent=1)
    return runtime


def engine_from_env() -> Engine:
    if os.environ.get("OCR_ENGINE", "fake") == "readingpipeline":
        return ReadingPipelineEngine(
            os.environ.get("OCR_WEIGHTS", "/app/weights"),
            threads=int(os.environ.get("OCR_THREADS", "2")),
        )
    return FakeEngine()
