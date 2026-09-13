"""Фото домашек пользователей на диске с TTL (арх. §6.3; на VPS вместо Object Storage).

Без исходников спорные случаи OCR («·» прочитано как «:», 850 → 860) не перепроверить.
152-ФЗ: фото — чувствительный артефакт, поэтому в имени только обезличенный id
пользователя (для удаления по запросу), а файлы старше `ttl_days` удаляются.
Раскладка: `<root>/<YYYY-MM-DD UTC>/<user>-<random>.<ext>`.
"""

import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PURGE_INTERVAL_S = 3600.0
_SIGNATURES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"RIFF", ".webp"))


class PhotoStore:
    def __init__(
        self, root: Path, ttl_days: int, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._root = root
        self._ttl_s = ttl_days * 86400.0
        self._clock = clock
        self._last_purge: float | None = None

    def save(self, user: str | None, data: bytes) -> str:
        """Сохраняет фото, возвращает ключ относительно корня (пишется в журнал событий)."""
        now = self._clock()
        if self._last_purge is None or now - self._last_purge >= PURGE_INTERVAL_S:
            self._last_purge = now
            try:
                self.purge_expired()
            except OSError:
                # сбой чистки не должен стоить фото текущей проверки
                logger.exception("photos purge failed")
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        key = f"{day}/{user or 'anon'}-{uuid.uuid4().hex[:12]}{_extension(data)}"
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def purge_expired(self) -> int:
        """Удаляет файлы старше TTL и опустевшие каталоги дней; возвращает число файлов."""
        if not self._root.is_dir():
            return 0
        deadline = self._clock() - self._ttl_s
        removed = 0
        for day_dir in self._root.iterdir():
            if not day_dir.is_dir():
                continue
            for photo in day_dir.iterdir():
                # файл могли удалить вручную (запрос пользователя) между iterdir и unlink
                with contextlib.suppress(FileNotFoundError):
                    if photo.is_file() and photo.stat().st_mtime < deadline:
                        photo.unlink()
                        removed += 1
            # в каталог мог прийти новый файл или его удалили параллельно — не ошибка
            with contextlib.suppress(OSError):
                if not any(day_dir.iterdir()):
                    day_dir.rmdir()
        if removed:
            logger.info("photos purged: %d older than %.0f days", removed, self._ttl_s / 86400)
        return removed


def _extension(data: bytes) -> str:
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    return ".bin"
