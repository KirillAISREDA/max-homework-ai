"""Polling-раннер (GET /updates, marker).

Обрабатывает апдейты последовательно: на пилоте один тестер, а PERS-тариф
GigaChat всё равно даёт 1 поток. Webhook и параллельная обработка — когда polling
перестанет справляться.

Остановка: SIGTERM (docker stop) → простаивающий long poll отменяется сразу, а уже
полученный батч дообрабатывается (marker к нему уже сдвинут — иначе сообщения
потеряются). SIGINT (Ctrl+C локально) — как раньше, KeyboardInterrupt.
"""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from types import FrameType

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


def _install_stop_handler(stop: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    # signal.signal, а не loop.add_signal_handler: последний не реализован на Windows,
    # где бот запускают для отладки. Хендлер выполняется в главном потоке между байткодами,
    # пока loop спит в select(); прямой stop.set() его не разбудит до конца long poll —
    # call_soon_threadsafe пишет в self-pipe и будит loop сразу.
    def _on_term(signum: int, frame: FrameType | None) -> None:
        logger.info("SIGTERM: finishing current batch, then exit")
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGTERM, _on_term)


async def _poll_loop(
    max_client: MaxClient, bot: Bot, marker_path: Path, stop: asyncio.Event
) -> None:
    marker = _load_marker(marker_path)
    stop_wait = asyncio.ensure_future(stop.wait())
    try:
        while not stop.is_set():
            poll = asyncio.ensure_future(max_client.get_updates(marker))
            await asyncio.wait({poll, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if not poll.done():
                # остановка во время простоя: marker не сдвинут, апдейты придут после рестарта
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll
                break
            try:
                updates, marker = poll.result()
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
    finally:
        stop_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_wait


async def run_polling(settings: Settings) -> None:
    if not settings.max_token:
        raise SystemExit("Не задан MAX_TOKEN (токен бота MAX, см. .env.example)")
    events = EventLog(Path(settings.events_path), settings.environment)
    store = InMemoryStateStore()
    # marker переживает рестарт: без него после падения бот либо перечитал бы
    # весь бэклог (дубли ответов и токены), либо потерял бы сообщения
    marker_path = Path(settings.events_path).parent / "max_marker.txt"
    stop = asyncio.Event()
    _install_stop_handler(stop, asyncio.get_running_loop())
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
        await _poll_loop(max_client, bot, marker_path, stop)
        logger.info("bot stopped")
