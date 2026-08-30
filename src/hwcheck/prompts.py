"""Загрузка версионированных промптов из prompts/ (арх. §4).

Версия промпта пишется в БД рядом с каждым результатом — для анализа качества.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(step: str, version: str = "v1") -> str:
    path = PROMPTS_DIR / step / f"{version}.md"
    return path.read_text(encoding="utf-8")
