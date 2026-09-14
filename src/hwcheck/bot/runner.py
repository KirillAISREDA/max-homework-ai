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

from redis.asyncio import Redis

from hwcheck.bot.fsm import InMemoryStateStore, RedisStateStore, StateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.max_api import MaxClient
from hwcheck.config import Settings
from hwcheck.crypto import UserIdCipher
from hwcheck.events import EventLog, set_id_hash_key
from hwcheck.llm.gigachat_client import GigaChatClient
from hwcheck.photos import PhotoStore

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


def _make_state_store(settings: Settings) -> tuple[StateStore, Redis | None]:
    """Redis, если задан REDIS_URL; клиент возвращается, чтобы закрыть его при остановке."""
    if not settings.redis_url:
        return InMemoryStateStore(), None
    # таймауты: зависший Redis даёт быстрое исключение, а не останавливает весь polling
    client = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    return RedisStateStore(client), client


def _make_photo_store(settings: Settings) -> PhotoStore | None:
    if settings.photos_ttl_days <= 0:
        return None
    return PhotoStore(Path(settings.photos_dir), settings.photos_ttl_days)


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


def configure_ids(settings: Settings) -> None:
    """Ключи id при старте: HMAC для обезличенных id; ключ шифра проверяется сразу, а не на
    первом пользователе (спецификация онбординга §10.1)."""
    set_id_hash_key(settings.id_hash_key or None)
    if settings.user_id_key:
        UserIdCipher(settings.user_id_key)
    if settings.environment == "prod" and not settings.id_hash_key:
        logger.warning("ID_HASH_KEY не задан: id обезличены legacy-хэшем, обратимым перебором")


async def run_polling(settings: Settings) -> None:
    if not settings.max_token:
        raise SystemExit("Не задан MAX_TOKEN (токен бота MAX, см. .env.example)")
    configure_ids(settings)
    events = EventLog(
        Path(settings.events_path), settings.environment, test_users=settings.test_user_hashes
    )
    store, redis_client = _make_state_store(settings)
    # marker переживает рестарт: без него после падения бот либо перечитал бы
    # весь бэклог (дубли ответов и токены), либо потерял бы сообщения
    marker_path = Path(settings.events_path).parent / "max_marker.txt"
    stop = asyncio.Event()
    _install_stop_handler(stop, asyncio.get_running_loop())
    if redis_client is not None:
        # Redis недоступен — лучше не стартовать (health покажет), чем терять диалоги молча
        await redis_client.ping()
    async with (
        MaxClient(
            settings.max_token, settings.max_base_url, ca_bundle=settings.max_ca_bundle
        ) as max_client,
        GigaChatClient(settings) as llm,
    ):
        me = await max_client.me()
        logger.info("bot started: %s", me.get("name") or me)
        print(f"Бот запущен: {me.get('name', me)}. Ctrl+C — остановка.")
        bot = Bot(max_client, llm, store, events, settings, photos=_make_photo_store(settings))
        try:
            await _poll_loop(max_client, bot, marker_path, stop)
        finally:
            if redis_client is not None:
                await redis_client.aclose()
        logger.info("bot stopped")
