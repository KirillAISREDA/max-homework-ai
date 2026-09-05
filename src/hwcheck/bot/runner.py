"""Polling-раннер для разработки (GET /updates, marker).

Обрабатывает апдейты последовательно: на пилоте один тестер, а PERS-тариф
GigaChat всё равно даёт 1 поток. Webhook и параллельная обработка — при деплое.
"""

import asyncio
import logging
from pathlib import Path

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.max_api import MaxClient
from hwcheck.config import Settings
from hwcheck.events import EventLog
from hwcheck.llm.gigachat_client import GigaChatClient

logger = logging.getLogger(__name__)


def _load_marker(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_marker(path: Path, marker: int | None) -> None:
    if marker is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(marker), encoding="utf-8")


async def run_polling(settings: Settings) -> None:
    if not settings.max_token:
        raise SystemExit("Не задан MAX_TOKEN (токен бота MAX, см. .env.example)")
    events = EventLog(Path(settings.events_path), settings.environment)
    store = InMemoryStateStore()
    # marker переживает рестарт: без него после падения бот либо перечитал бы
    # весь бэклог (дубли ответов и токены), либо потерял бы сообщения
    marker_path = Path(settings.events_path).parent / "max_marker.txt"
    async with (
        MaxClient(
            settings.max_token, settings.max_base_url, ca_bundle=settings.max_ca_bundle
        ) as max_client,
        GigaChatClient(settings) as llm,
    ):
        me = await max_client.me()
        logger.info("bot started: %s", me.get("name") or me)
        print(f"Бот запущен: {me.get('name', me)}. Ctrl+C — остановка.")
        bot = Bot(max_client, llm, store, events, settings)
        marker = _load_marker(marker_path)
        while True:
            try:
                updates, marker = await max_client.get_updates(marker)
                _save_marker(marker_path, marker)
            except Exception:
                logger.exception("get_updates failed, retry in 5s")
                await asyncio.sleep(5)
                continue
            for update in updates:
                try:
                    await bot.handle_update(update)
                except Exception:
                    # один сбойный апдейт не должен ронять цикл
                    logger.exception("update failed: %s", update.update_type)
