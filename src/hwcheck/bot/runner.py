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


async def run_polling(settings: Settings) -> None:
    if not settings.max_token:
        raise SystemExit("Не задан MAX_TOKEN (токен бота MAX, см. .env.example)")
    events = EventLog(Path(settings.events_path), settings.environment)
    store = InMemoryStateStore()
    async with (
        MaxClient(settings.max_token, settings.max_base_url) as max_client,
        GigaChatClient(settings) as llm,
    ):
        me = await max_client.me()
        logger.info("bot started: %s", me.get("name") or me)
        print(f"Бот запущен: {me.get('name', me)}. Ctrl+C — остановка.")
        bot = Bot(max_client, llm, store, events, settings)
        marker: int | None = None
        while True:
            try:
                updates, marker = await max_client.get_updates(marker)
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
