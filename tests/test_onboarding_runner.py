"""Старт бота с онбордингом (спецификация §11, §13): без базы, ключей, username — не стартует."""

from pathlib import Path
from typing import Any

import fakeredis
import pytest

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.bot.onboarding.state import RedisOnboardingStateStore
from hwcheck.bot.runner import check_onboarding_settings, make_onboarding
from hwcheck.config import Settings
from hwcheck.crypto import new_user_id_key
from hwcheck.events import EventLog


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "onboarding_required": True,
        "database_url": "postgresql://homework@db/homework",
        "id_hash_key": "secret",
        "user_id_key": new_user_id_key(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_onboarding_needs_database_and_id_keys() -> None:
    check_onboarding_settings(Settings(_env_file=None))  # флаг выключен — ничего не нужно
    check_onboarding_settings(settings())
    with pytest.raises(SystemExit, match="DATABASE_URL, USER_ID_KEY"):
        check_onboarding_settings(settings(database_url=None, user_id_key=""))


def test_make_onboarding(tmp_path: Path) -> None:
    # тесты не проверяются mypy: вместо пула и клиента MAX — заглушки
    common: dict[str, Any] = {
        "redis_client": fakeredis.FakeAsyncRedis(),
        "dialogs": InMemoryStateStore(),
        "max_client": object(),
        "events": EventLog(tmp_path / "events.jsonl", "dev"),
    }
    off = make_onboarding(Settings(_env_file=None), pool=None, me={"username": "bot"}, **common)
    assert off is None
    with pytest.raises(SystemExit, match="username"):
        make_onboarding(settings(), pool=object(), me={"name": "Домашка"}, **common)
    with pytest.raises(SystemExit, match="PostgreSQL"):
        make_onboarding(settings(), pool=None, me={"username": "bot"}, **common)
    onboarding = make_onboarding(
        settings(), pool=object(), me={"username": "domashka_bot"}, **common
    )
    assert isinstance(onboarding, Onboarding)
    assert onboarding._ctx.bot_username == "domashka_bot"
    assert isinstance(onboarding._ctx.states, RedisOnboardingStateStore)
