import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot, _pseudo_ref, _validator_only_grade
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.models import MaxUpdate
from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize

PHOTO_UPDATE = {
    "update_type": "message_created",
    "timestamp": 1,
    "message": {
        "sender": {"user_id": 42, "name": "Ученик"},
        "recipient": {"chat_id": 7},
        "body": {
            "mid": "m1",
            "text": None,
            "attachments": [{"type": "image", "payload": {"url": "https://files/1.jpg"}}],
        },
    },
}
TEXT_UPDATE = {
    "update_type": "message_created",
    "message": {
        "sender": {"user_id": 42},
        "recipient": {"chat_id": 7},
        "body": {"mid": "m2", "text": "привет", "attachments": []},
    },
}
UNKNOWN_UPDATE = {"update_type": "chat_title_changed", "something": {"weird": 1}}


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Buttons | None]] = []
        self.callbacks: list[str] = []

    async def send_message(
        self, chat_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        self.callbacks.append(callback_id)

    async def download(self, url: str) -> bytes:
        return b"fake-image"


def make_bot(tmp_path: Path) -> tuple[Bot, FakeMax, Path]:
    events_path = tmp_path / "events.jsonl"
    fake_max = FakeMax()
    settings = Settings(_env_file=None)
    bot = Bot(
        fake_max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]  # LLM не нужен для этих сценариев
        InMemoryStateStore(),
        EventLog(events_path, "dev"),
        settings,
    )
    return bot, fake_max, events_path


def test_update_parsing_tolerates_unknown_fields() -> None:
    update = MaxUpdate.model_validate(UNKNOWN_UPDATE)
    assert update.update_type == "chat_title_changed"
    assert update.effective_chat_id is None

    photo = MaxUpdate.model_validate(PHOTO_UPDATE)
    assert photo.effective_chat_id == 7
    assert photo.effective_user_id == 42
    assert photo.message is not None
    assert photo.message.image_urls == ["https://files/1.jpg"]


async def test_text_in_idle_sends_welcome(tmp_path: Path) -> None:
    bot, fake_max, events_path = make_bot(tmp_path)
    await bot.handle_update(MaxUpdate.model_validate(TEXT_UPDATE))
    assert len(fake_max.sent) == 1
    assert "фото" in fake_max.sent[0][1]
    # событие записано с обезличенным id и environment
    record = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["env"] == "dev"
    assert record["user"] == anonymize(42)
    assert record["user"] != "42"


async def test_unknown_update_ignored(tmp_path: Path) -> None:
    bot, fake_max, _ = make_bot(tmp_path)
    await bot.handle_update(MaxUpdate.model_validate(UNKNOWN_UPDATE))
    assert fake_max.sent == []


def test_validator_only_grade() -> None:
    result = _validator_only_grade(["999+1=1000", "950+50-660=320"])
    assert result.verdict == "wrong"
    assert result.first_error_line == 2

    assert _validator_only_grade(["999+1=1000"]).verdict == "correct"
    assert _validator_only_grade(["<неразборчиво>"]).verdict == "uncertain"


def test_pseudo_ref_uses_computed_value() -> None:
    result = _validator_only_grade(["950+50-660=320"])
    ref = _pseudo_ref(result)
    assert ref.answer == "340"  # правильное значение, посчитанное валидатором


def test_anonymize_stable_and_irreversible() -> None:
    assert anonymize(42) == anonymize(42)
    assert anonymize(42) != anonymize(43)
    assert anonymize(None) is None
    value = anonymize(42)
    assert value is not None and "42" not in value


@pytest.mark.parametrize("payload", ["tutor:99", "unknown"])
async def test_callback_out_of_range_is_safe(tmp_path: Path, payload: str) -> None:
    bot, fake_max, _ = make_bot(tmp_path)
    update: dict[str, Any] = {
        "update_type": "message_callback",
        "chat_id": 7,
        "callback": {"callback_id": "cb1", "payload": payload, "user": {"user_id": 42}},
    }
    await bot.handle_update(MaxUpdate.model_validate(update))
    assert fake_max.callbacks == ["cb1"]
    assert fake_max.sent == []
