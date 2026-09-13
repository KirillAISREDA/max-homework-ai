"""Лог бота в файле на томе var/: логи контейнера пропадают при каждой пересборке
(13.09 структуру страниц спорных проверок было уже не посмотреть)."""

import logging
from pathlib import Path

from hwcheck.cli import configure_bot_logging
from hwcheck.config import Settings


def test_bot_log_goes_to_rotating_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    log_path = tmp_path / "logs" / "bot.log"
    try:
        configure_bot_logging(Settings(_env_file=None, log_path=str(log_path)))
        logging.getLogger("hwcheck.bot.handlers").info("page role=notebook")
        logging.getLogger("httpx").info("GET /updates 200")  # шум long polling — не пишем
        for handler in root.handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "page role=notebook" in text
        assert "GET /updates" not in text
    finally:
        for handler in root.handlers:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
