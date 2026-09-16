"""Движки распознавания рукописи: контракт и фейк. ReadingPipeline — в этапе «русский»."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol


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


class ReadingPipelineEngine:
    name = "readingpipeline"

    def __init__(self, weights_dir: str) -> None:
        self._weights_dir = weights_dir

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError("подключается в этапе 3 по итогам спайка ReadingPipeline")


def engine_from_env() -> Engine:
    if os.environ.get("OCR_ENGINE", "fake") == "readingpipeline":
        return ReadingPipelineEngine(os.environ.get("OCR_WEIGHTS", "/app/weights"))
    return FakeEngine()
