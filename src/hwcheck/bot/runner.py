"""Polling-раннер (GET /updates, marker).

Обрабатывает апдейты последовательно: на пилоте один тестер, а PERS-тариф
GigaChat всё равно даёт 1 поток. Webhook и параллельная обработка — когда polling
перестанет справляться.

Остановка: SIGTERM (docker stop) → простаивающий long poll отменяется сразу, а уже
полученный батч дообрабатывается (marker к нему уже сдвинут — иначе сообщения
потеряются). SIGINT (Ctrl+C локально) — как раньше, KeyboardInterrupt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from types import FrameType
from typing import Any

import asyncpg
from redis.asyncio import Redis

from hwcheck.bot.fsm import InMemoryStateStore, RedisStateStore, StateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.max_api import MaxClient
from hwcheck.bot.onboarding.context import OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION, policy_messages
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.bot.onboarding.state import (
    InMemoryOnboardingStateStore,
    OnboardingStateStore,
    RedisOnboardingStateStore,
)
from hwcheck.config import Settings
from hwcheck.crypto import UserIdCipher, UserIdCipherError
from hwcheck.db.pool import create_pool
from hwcheck.db.repo import PgProfileRepository
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


def check_onboarding_settings(settings: Settings) -> None:
    """ONBOARDING_REQUIRED без базы или ключей id — бот не стартует с понятной ошибкой (§11)."""
    if not settings.onboarding_required:
        return
    required = {
        "DATABASE_URL": settings.database_url,
        "ID_HASH_KEY": settings.id_hash_key,
        "USER_ID_KEY": settings.user_id_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"ONBOARDING_REQUIRED=true: не заданы {', '.join(missing)}")


def make_onboarding(
    settings: Settings,
    *,
    pool: asyncpg.Pool[asyncpg.Record] | None,
    redis_client: Redis | None,
    dialogs: StateStore,
    max_client: MaxClient,
    events: EventLog,
    me: dict[str, Any],
) -> Onboarding | None:
    """Онбординг перед проверкой; None — флаг выключен (аварийный выключатель, спецификация §7)."""
    if not settings.onboarding_required:
        return None
    username = me.get("username")
    if not isinstance(username, str) or not username:
        raise SystemExit(
            "ONBOARDING_REQUIRED=true: у бота нет username в GET /me — ссылки не собрать"
        )
    if pool is None:
        raise SystemExit("ONBOARDING_REQUIRED=true: нет подключения к PostgreSQL")
    try:
        policy_messages()  # «Полный текст» на экране согласия: без файла политики не стартуем
    except FileNotFoundError as exc:
        raise SystemExit(
            "ONBOARDING_REQUIRED=true: нет текста политики "
            f"docs/legal/privacy-policy-{POLICY_VERSION}.md"
        ) from exc
    states: OnboardingStateStore = (
        RedisOnboardingStateStore(redis_client)
        if redis_client is not None
        else InMemoryOnboardingStateStore()
    )
    ctx = OnboardingContext(
        max=max_client,
        repo=PgProfileRepository(pool),
        states=states,
        dialogs=dialogs,
        events=events,
        cipher=UserIdCipher(settings.user_id_key),
        bot_username=username,
    )
    return Onboarding(ctx)


def log_onboarding_mode(settings: Settings, onboarding: Onboarding | None) -> None:
    """Режим онбординга в лог; выключенный флаг в prod — предупреждение, а не молчание."""
    logger.info("onboarding: %s", "required" if onboarding is not None else "off")
    if onboarding is None and settings.environment == "prod":
        logger.warning("ONBOARDING_REQUIRED=false в prod: фото проверяются без согласия родителя")


async def run_polling(settings: Settings) -> None:
    if not settings.max_token:
        raise SystemExit("Не задан MAX_TOKEN (токен бота MAX, см. .env.example)")
    try:
        configure_ids(settings)
    except UserIdCipherError as exc:
        raise SystemExit(str(exc)) from exc
    check_onboarding_settings(settings)
    events = EventLog(
        Path(settings.events_path), settings.environment, test_users=settings.test_user_hashes
    )
    store, redis_client = _make_state_store(settings)
    # marker переживает рестарт: без него после падения бот либо перечитал бы
    # весь бэклог (дубли ответов и токены), либо потерял бы сообщения
    marker_path = Path(settings.events_path).parent / "max_marker.txt"
    stop = asyncio.Event()
    _install_stop_handler(stop, asyncio.get_running_loop())
    pool: asyncpg.Pool[asyncpg.Record] | None = None
    # всё открытое закрывается в обратном порядке, даже если старт MAX/GigaChat упал (ревью)
    async with contextlib.AsyncExitStack() as resources:
        if redis_client is not None:
            resources.push_async_callback(redis_client.aclose)
            # Redis недоступен — лучше не стартовать (health покажет), чем терять диалоги молча
            await redis_client.ping()
        if settings.database_url:
            # PostgreSQL недоступен или миграция упала — не стартуем, профили не потеряются
            pool = await create_pool(settings.database_url)
            resources.push_async_callback(pool.close)
        max_client = await resources.enter_async_context(
            MaxClient(settings.max_token, settings.max_base_url, ca_bundle=settings.max_ca_bundle)
        )
        llm = await resources.enter_async_context(GigaChatClient(settings))
        me = await max_client.me()
        logger.info("bot started: %s", me.get("name") or me)
        print(f"Бот запущен: {me.get('name', me)}. Ctrl+C — остановка.")
        onboarding = make_onboarding(
            settings,
            pool=pool,
            redis_client=redis_client,
            dialogs=store,
            max_client=max_client,
            events=events,
            me=me,
        )
        log_onboarding_mode(settings, onboarding)
        bot = Bot(
            max_client,
            llm,
            store,
            events,
            settings,
            photos=_make_photo_store(settings),
            onboarding=onboarding,
        )
        await _poll_loop(max_client, bot, marker_path, stop)
    logger.info("bot stopped")
