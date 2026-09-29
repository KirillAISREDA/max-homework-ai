"""Загрузка версионированных промптов из prompts/ (арх. §4).

Версия промпта пишется в БД рядом с каждым результатом — для анализа качества.
"""

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


# версия — имя файла: строчные буквы, цифры и дефис, без путей и расширений
_VERSION = re.compile(r"[a-z0-9][a-z0-9-]{0,40}", flags=re.ASCII)


def require_prompt(step: str, version: str) -> str:
    """Версия из настроек или конфигурации стенда: такой промпт есть, иначе ValueError.

    Проверяется до первого вызова модели: опечатка всплыла бы на первом фото, а путь вместо
    версии молча подставил бы промпт другого шага.
    """
    known = _VERSION.fullmatch(version) is not None
    if not known or not (PROMPTS_DIR / step / f"{version}.md").is_file():
        raise ValueError(f"нет промпта prompts/{step}/{version}.md")
    return version


def load_prompt(step: str, version: str = "v1") -> str:
    path = PROMPTS_DIR / step / f"{version}.md"
    return path.read_text(encoding="utf-8")
