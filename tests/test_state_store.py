"""Redis StateStore: открытые разборы тьютора переживают рестарт контейнера."""

from pathlib import Path

import fakeredis

from hwcheck.bot.fsm import ChatState, InMemoryStateStore, RedisStateStore
from hwcheck.bot.runner import _make_photo_store, _make_state_store
from hwcheck.config import Settings
from hwcheck.events import anonymize
from hwcheck.pipeline.schemas import VisionTask


def sample_state() -> ChatState:
    return ChatState(
        phase="review",
        textbook_tasks=[VisionTask(number=19, task_text="700 - (220 + 180)", confidence=0.9)],
        textbook_saved_at=1_789_300_000.0,
        resolved_indices=[1],
    )


async def test_state_survives_new_store_instance() -> None:
    server = fakeredis.FakeServer()
    await RedisStateStore(fakeredis.FakeAsyncRedis(server=server)).set(7, sample_state())
    # новый клиент к тому же серверу — как бот после рестарта контейнера
    restored = await RedisStateStore(fakeredis.FakeAsyncRedis(server=server)).get(7)
    assert restored == sample_state()


async def test_missing_state_is_fresh_idle() -> None:
    store = RedisStateStore(fakeredis.FakeAsyncRedis())
    assert await store.get(7) == ChatState()


async def test_state_key_has_ttl_and_no_raw_chat_id() -> None:
    client = fakeredis.FakeAsyncRedis()
    await RedisStateStore(client, ttl_s=3600).set(123456789, sample_state())
    keys = [k.decode() for k in await client.keys("*")]
    assert keys == [f"fsm:{anonymize(123456789)}"]
    assert "123456789" not in keys[0]
    assert 0 < await client.ttl(keys[0]) <= 3600


async def test_corrupted_state_falls_back_to_idle() -> None:
    """Схема ChatState поменялась между деплоями — чат начинает заново, бот не падает."""
    client = fakeredis.FakeAsyncRedis()
    await client.set(f"fsm:{anonymize(7)}", b'{"phase": "unknown-phase"}')
    assert await RedisStateStore(client).get(7) == ChatState()


def test_runner_picks_store_by_settings(tmp_path: Path) -> None:
    store, _ = _make_state_store(Settings(_env_file=None))
    assert isinstance(store, InMemoryStateStore)
    store, client = _make_state_store(Settings(_env_file=None, redis_url="redis://redis:6379/0"))
    assert isinstance(store, RedisStateStore)
    assert client is not None
    # зависший Redis должен давать быстрое исключение, а не останавливать весь polling
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == kwargs["socket_connect_timeout"] == 5


def test_runner_photo_store_disabled_by_zero_ttl(tmp_path: Path) -> None:
    assert _make_photo_store(Settings(_env_file=None, photos_ttl_days=0)) is None
    assert _make_photo_store(Settings(_env_file=None, photos_dir=str(tmp_path))) is not None
