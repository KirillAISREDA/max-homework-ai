import json
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import RETRY, Bot, _pseudo_ref, _validator_only_grade
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.models import MaxUpdate
from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize
from hwcheck.photos import PhotoStore

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
        self.image_tokens: list[str | None] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Buttons | None = None,
        image_token: str | None = None,
    ) -> None:
        self.sent.append((chat_id, text, buttons))
        self.image_tokens.append(image_token)

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        self.callbacks.append(callback_id)

    async def download(self, url: str) -> bytes:
        return b"fake-image"

    async def upload_image(self, image: bytes) -> str:
        return "tok"


def make_bot(tmp_path: Path, photos: PhotoStore | None = None) -> tuple[Bot, FakeMax, Path]:
    events_path = tmp_path / "events.jsonl"
    fake_max = FakeMax()
    settings = Settings(_env_file=None)
    bot = Bot(
        fake_max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]  # LLM не нужен для этих сценариев
        InMemoryStateStore(),
        EventLog(events_path, "dev"),
        settings,
        photos=photos,
    )
    return bot, fake_max, events_path


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


async def test_photo_saved_before_recognition_and_failure_traced(tmp_path: Path) -> None:
    """Фото сохраняется до vision (упавшие случаи — самые ценные для разбора),
    все события апдейта связаны одним trace_id, у сбоев есть код ошибки."""
    photos_root = tmp_path / "photos"
    bot, fake_max, events_path = make_bot(tmp_path, photos=PhotoStore(photos_root, ttl_days=30))
    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))  # LLM нет → vision падает

    events = read_events(events_path)
    assert [e["type"] for e in events] == ["homework_uploaded", "photo_failed", "check_failed"]
    trace_ids = {e["trace_id"] for e in events}
    assert len(trace_ids) == 1 and None not in trace_ids
    failed = events[1]
    assert failed["error"]
    assert (photos_root / failed["photo"]).read_bytes() == b"fake-image"
    assert failed["photo"].split("/")[1].startswith(f"{anonymize(42)}-")
    assert events[2]["error"] == "RuntimeError"
    assert fake_max.sent[-1][1] == RETRY


async def test_photo_store_failure_does_not_break_check(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("файл вместо каталога", encoding="utf-8")
    bot, fake_max, events_path = make_bot(tmp_path, photos=PhotoStore(blocker, ttl_days=30))
    await bot.handle_update(MaxUpdate.model_validate(PHOTO_UPDATE))

    events = read_events(events_path)
    failed = next(e for e in events if e["type"] == "photo_failed")
    assert failed["photo"] is None  # фото не сохранилось, но распознавание всё равно пробовали
    assert fake_max.sent[-1][1] == RETRY


class BrokenStore:
    """Redis недоступен посреди диалога."""

    async def get(self, chat_id: int) -> Any:
        raise ConnectionError("redis down")

    async def set(self, chat_id: int, state: Any) -> None:
        raise ConnectionError("redis down")


@pytest.mark.parametrize(
    "update",
    [
        TEXT_UPDATE,
        {
            "update_type": "message_callback",
            "chat_id": 7,
            "callback": {"callback_id": "cb1", "payload": "tutor:0", "user": {"user_id": 42}},
        },
    ],
)
async def test_store_failure_in_dialog_replies_retry(
    tmp_path: Path, update: dict[str, Any]
) -> None:
    """Сбой хранилища на тексте/кнопке: ребёнок получает «попробуй ещё раз», а не тишину."""
    bot, fake_max, events_path = make_bot(tmp_path)
    bot._store = BrokenStore()  # type: ignore[assignment]
    await bot.handle_update(MaxUpdate.model_validate(update))

    assert fake_max.sent[-1][1] == RETRY
    if update["update_type"] == "message_callback":
        assert fake_max.callbacks == ["cb1"]  # кнопка в MAX не «крутится» бесконечно
    failed = [e for e in read_events(events_path) if e["type"] == "update_failed"]
    assert len(failed) == 1 and failed[0]["error"] == "ConnectionError"


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


async def test_findings_logged_and_saved(tmp_path: Path) -> None:
    """Каждая находка — событие finding_created и запись в репозитории; всё в одном trace_id."""
    from hwcheck.bot.fsm import ChatState
    from hwcheck.db.findings import InMemoryFindingsRepository
    from hwcheck.pipeline.schemas import VisionTask

    bot, fake_max, events_path = make_bot(tmp_path)
    findings = InMemoryFindingsRepository()
    bot._findings = findings
    task = VisionTask(number=7, task_text="", student_solution_steps=["2 + 2 = 5"], confidence=1)
    checked = await bot._check_task(42, task)
    assert checked.grade.verdict == "wrong" and checked.findings[0].strength == "verified"
    [record] = findings.saved
    assert (record.user_hash, record.task_number, record.kind) == (anonymize(42), "7", "arithmetic")
    created = [e for e in read_events(events_path) if e["type"] == "finding_created"]
    assert [(e["subject"], e["strength"]) for e in created] == [("math", "verified")]
    assert record.trace_id is None  # trace_id есть только внутри handle_update
    await bot._store.set(7, ChatState(phase="review", tasks=[checked]))


def test_task_result_of_trust_follows_ref_status() -> None:
    """Доверие эталону в разборе — по статусу солвера, а не по факту «эталон не пуст»."""
    from hwcheck.bot.fsm import CheckedTask
    from hwcheck.bot.handlers import task_result_of
    from hwcheck.pipeline.schemas import VisionTask
    from hwcheck.pipeline.solver import RefSolution

    steps = ["220 + 180 = 400"]
    ref = RefSolution(steps=steps, answer="400")
    task = VisionTask(number=19, task_text="Сколько?", student_solution_steps=steps, confidence=1)
    unverified = CheckedTask(
        task=task, ref=ref, grade=_validator_only_grade(steps), ref_status="ref_not_verified"
    )
    result = task_result_of(0, unverified)
    assert result.reference is not None and result.reference.trust == "unverified"
    assert result.payload["ref_status"] == "ref_not_verified"

    verified = unverified.model_copy(update={"ref_status": "ok"})
    verified_result = task_result_of(0, verified)
    assert verified_result.reference is not None
    assert verified_result.reference.trust == "verified"
    assert verified_result.payload["ref_status"] == "ok"


def test_anonymize_stable_and_irreversible() -> None:
    assert anonymize(42) == anonymize(42)
    assert anonymize(42) != anonymize(43)
    assert anonymize(None) is None
    value = anonymize(42)
    assert value is not None and "42" not in value


def test_remaining_buttons_exclude_resolved() -> None:
    from hwcheck.bot.fsm import ChatState, CheckedTask
    from hwcheck.bot.handlers import _remaining_buttons
    from hwcheck.pipeline.schemas import VisionTask

    def wrong_task(number: int) -> CheckedTask:
        return CheckedTask(
            task=VisionTask(number=number, task_text="", confidence=0.9),
            ref=None,
            grade=_validator_only_grade(["2+2=5"]),
        )

    state = ChatState(tasks=[wrong_task(4), wrong_task(7)], resolved_indices=[0])
    buttons = _remaining_buttons(state)
    # разобранное задание №4 (индекс 0) не предлагается повторно
    assert len(buttons) == 1
    assert buttons[0][0]["payload"] == "tutor:1"
    assert buttons[0][0]["text"] == "Разобрать №7"

    unnumbered = state.tasks[1].model_copy(
        update={"task": state.tasks[1].task.model_copy(update={"number_on_page": False})}
    )
    state = state.model_copy(update={"tasks": [state.tasks[0], unnumbered]})
    assert _remaining_buttons(state)[0][0]["text"] == "Разобрать задание 7"


def test_parse_tutor_index_rejects_garbage() -> None:
    from hwcheck.bot.handlers import _parse_tutor_index

    assert _parse_tutor_index("tutor:1", 3) == 1
    assert _parse_tutor_index("tutor:-1", 3) is None  # отрицательный индекс не оборачивается
    assert _parse_tutor_index("tutor:x", 3) is None
    assert _parse_tutor_index("tutor:99", 3) is None
    assert _parse_tutor_index("tutor:", 3) is None


def test_marker_persistence(tmp_path: Path) -> None:
    from hwcheck.bot.runner import _load_marker, _save_marker

    path = tmp_path / "marker.txt"
    assert _load_marker(path) is None
    _save_marker(path, 12345)
    assert _load_marker(path) == 12345
    path.write_text("мусор", encoding="utf-8")
    assert _load_marker(path) is None


@pytest.mark.parametrize("payload", ["tutor:99", "unknown", "tutor:-1", "tutor:abc"])
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


# --- graceful shutdown раннера (docker stop → SIGTERM) ---


class FakeBotSink:
    def __init__(self) -> None:
        self.handled: list[str] = []

    async def handle_update(self, update: MaxUpdate) -> None:
        self.handled.append(update.update_type)


async def test_poll_loop_finishes_fetched_batch_after_stop(tmp_path: Path) -> None:
    """Marker сдвинут вместе с полученным батчем — батч дообрабатывается даже при stop."""
    import asyncio

    from hwcheck.bot.runner import _load_marker, _poll_loop

    stop = asyncio.Event()

    class Poller:
        calls = 0

        async def get_updates(
            self, marker: int | None, *, timeout: int = 30
        ) -> tuple[list[MaxUpdate], int | None]:
            self.calls += 1
            stop.set()  # SIGTERM пришёл, пока ответ уже в пути
            return [MaxUpdate.model_validate(u) for u in (UNKNOWN_UPDATE, TEXT_UPDATE)], 777

    poller, sink = Poller(), FakeBotSink()
    marker_path = tmp_path / "marker.txt"
    await asyncio.wait_for(
        _poll_loop(poller, sink, marker_path, stop),  # type: ignore[arg-type]
        timeout=5,
    )
    assert poller.calls == 1
    assert sink.handled == ["chat_title_changed", "message_created"]
    assert _load_marker(marker_path) == 777


async def test_poll_loop_cancels_idle_long_poll_on_stop(tmp_path: Path) -> None:
    """Простаивающий long poll (до 30 с) не задерживает остановку; marker не трогаем."""
    import asyncio

    from hwcheck.bot.runner import _poll_loop

    stop = asyncio.Event()

    class Poller:
        cancelled = False

        async def get_updates(
            self, marker: int | None, *, timeout: int = 30
        ) -> tuple[list[MaxUpdate], int | None]:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return [], 1

    poller, sink = Poller(), FakeBotSink()
    marker_path = tmp_path / "marker.txt"
    task = asyncio.create_task(_poll_loop(poller, sink, marker_path, stop))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert poller.cancelled
    assert sink.handled == []
    assert not marker_path.exists()


async def test_stop_handler_wakes_loop_and_sets_event() -> None:
    import asyncio
    import signal

    from hwcheck.bot.runner import _install_stop_handler

    stop = asyncio.Event()
    previous = signal.getsignal(signal.SIGTERM)
    try:
        _install_stop_handler(stop, asyncio.get_running_loop())
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        # set() приходит через call_soon_threadsafe — ждём его, а не крутим цикл вручную
        await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        signal.signal(signal.SIGTERM, previous)


async def test_text_after_review_points_to_buttons_not_welcome(tmp_path: Path) -> None:
    """Живой альбом 14.09: текст после сводки получал приветствие — будто бот всё забыл."""
    from hwcheck.bot.fsm import ChatState, CheckedTask
    from hwcheck.bot.handlers import REVIEW_DONE, REVIEW_HINT, WELCOME
    from hwcheck.pipeline.schemas import VisionTask

    bot, fake_max, _ = make_bot(tmp_path)
    steps = ["2 + 2 = 5"]
    task = VisionTask(number=55, task_text="", student_solution_steps=steps, confidence=1)
    wrong = CheckedTask(task=task, ref=None, grade=_validator_only_grade(steps))
    store = bot._store
    await store.set(7, ChatState(phase="review", tasks=[wrong]))

    await bot.handle_update(MaxUpdate.model_validate(TEXT_UPDATE))
    _chat, text, buttons = fake_max.sent[-1]
    assert text == REVIEW_HINT != WELCOME
    assert buttons is not None and buttons[0][0]["payload"] == "tutor:0"

    await store.set(7, ChatState(phase="review", tasks=[wrong], resolved_indices=[0]))
    await bot.handle_update(MaxUpdate.model_validate(TEXT_UPDATE))
    assert fake_max.sent[-1][1:] == (REVIEW_DONE, None)
