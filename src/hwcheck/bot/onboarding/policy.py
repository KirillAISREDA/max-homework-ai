"""Политика обработки персональных данных (спецификация онбординга §10.3).

Полный текст — `docs/legal/privacy-policy-<версия>.md`, версия пишется в каждое согласие. Новый
текст — новый файл и новая POLICY_VERSION: старые согласия остаются со своей версией.
"""

from pathlib import Path

POLICY_VERSION = "v0"  # черновик: до вычитки юристом реальных пользователей не привлекаем (§14)
LEGAL_DIR = Path(__file__).resolve().parents[4] / "docs" / "legal"
MAX_MESSAGE_LEN = 4000  # лимит текста сообщения MAX


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
