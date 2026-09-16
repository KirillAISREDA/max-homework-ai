"""Временное состояние онбординга в Redis (спецификация §4, §4.7).

TTL, ключ без id, сброс битого.
"""

import fakeredis

from hwcheck.bot.onboarding.state import (
    InMemoryOnboardingStateStore,
    OnboardingState,
    RedisOnboardingStateStore,
)


def sample() -> OnboardingState:
    return OnboardingState(
        pending_invite="t" * 64,
        pending_photos=["https://files/1.jpg"],
        pending_at=1_789_300_000.0,
        child_id=5,
        child_chosen_at=1_789_300_100.0,
    )


async def test_state_survives_new_client_and_has_ttl() -> None:
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server)
    await RedisOnboardingStateStore(client, ttl_s=3600).set("abc123", sample())
    restored = await RedisOnboardingStateStore(fakeredis.FakeAsyncRedis(server=server)).get(
        "abc123"
    )
    assert restored == sample()
    assert [k.decode() for k in await client.keys("*")] == ["onb:abc123"]
    assert 0 < await client.ttl("onb:abc123") <= 3600


async def test_missing_or_broken_state_is_empty() -> None:
    client = fakeredis.FakeAsyncRedis()
    store = RedisOnboardingStateStore(client)
    assert await store.get("nobody") == OnboardingState()
    await client.set("onb:broken", '{"pending_photos": "не список"}')
    assert await store.get("broken") == OnboardingState()


async def test_in_memory_store() -> None:
    store = InMemoryOnboardingStateStore()
    assert await store.get("u") == OnboardingState()
    await store.set("u", sample())
    assert await store.get("u") == sample()
