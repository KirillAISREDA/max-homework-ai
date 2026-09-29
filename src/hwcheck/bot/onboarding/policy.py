"""Политика обработки персональных данных (спецификация онбординга §10.3).

Полный текст — `docs/legal/privacy-policy-<версия>.md`, версия пишется в каждое согласие. Новый
текст — новый файл и новая POLICY_VERSION: старые согласия остаются со своей версией.
"""

import re
from pathlib import Path

# v1 (28.09) — оператор назван, описаны уведомления родителю; v2 (29.09) — фото страниц читает
# модель стороннего разработчика (трансграничная передача). Вычитка юристом идёт параллельно,
# её правки — следующая версия
POLICY_VERSION = "v2"
# с этой версии политика говорит родителю о передаче фото сторонней модели
FOREIGN_MODELS_SINCE = 2
LEGAL_DIR = Path(__file__).resolve().parents[4] / "docs" / "legal"
MAX_MESSAGE_LEN = 4000  # лимит текста сообщения MAX


def allows_foreign_models(version: str | None) -> bool:
    """Согласие по этой версии политики покрывает передачу фото сторонней модели.

    Нет согласия или версия записана непонятно — не покрывает: фото читает GigaChat.
    """
    # номер версии короткий: строка из тысяч цифр — не версия, и `int` на ней падает
    match = re.fullmatch(r"v(\d{1,4})", version or "", flags=re.ASCII)
    return match is not None and int(match.group(1)) >= FOREIGN_MODELS_SINCE


def policy_messages(version: str = POLICY_VERSION) -> list[str]:
    text = (LEGAL_DIR / f"privacy-policy-{version}.md").read_text(encoding="utf-8")
    return split_message(text)


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Сообщения не длиннее лимита: режутся по абзацам, слишком длинный абзац — по лимиту."""
    chunks: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        for start in range(0, len(paragraph), limit):
            piece = paragraph[start : start + limit]
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks
