"""Сценарий бота «учебник + тетрадь»: альбом из двух фото, учебник отдельным
сообщением, порядок фото, лимит фото. Vision и Solver подменены."""

import logging
import time
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot import handlers
from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.models import MaxUpdate
from hwcheck.config import Settings
from hwcheck.events import EventLog
from hwcheck.pipeline.schemas import VisionPage
from hwcheck.pipeline.solver import RefSolution, SolvedTask
from hwcheck.pipeline.vision import RecognizedPage
from test_bot import FakeMax
from test_pages import NOTEBOOK_19, TEXTBOOK

PAGES: dict[bytes, VisionPage] = {
    b"textbook": VisionPage(tasks=TEXTBOOK, page_ok=True),
    b"notebook": VisionPage(tasks=NOTEBOOK_19, page_ok=True),
    b"blank": VisionPage(tasks=[], page_ok=False, page_comment="пусто"),
    b"bare": VisionPage(tasks=[NOTEBOOK_19[0].model_copy(update={"task_text": ""})], page_ok=True),
}

Harness = tuple[Bot, "FakeMaxPerUrl", InMemoryStateStore, list[str]]


class FakeMaxPerUrl(FakeMax):
    def __init__(self) -> None:
        super().__init__()
        self.downloaded: list[str] = []

    async def download(self, url: str) -> bytes:
        self.downloaded.append(url)
        name = url.rsplit("/", 1)[-1]
        if name == "broken":
            raise RuntimeError("download failed")
        return name.encode()


def photo_update(*names: str, chat_id: int = 7) -> MaxUpdate:
    attachments = [{"type": "image", "payload": {"url": f"https://files/{n}"}} for n in names]
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": chat_id},
                "body": {"mid": "m", "text": "", "attachments": attachments},
            },
        }
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    solved_texts: list[str] = []

    async def fake_recognize(_client: Any, image: bytes, **_kw: Any) -> RecognizedPage:
        return RecognizedPage(
            page=PAGES[image],
            orientation=0,
            attempts=0,
            tokens_in=1,
            tokens_out=1,
            latency_s=0.0,
            raw="",
        )

    async def fake_solve(_client: Any, task_text: str, **_kw: Any) -> tuple[SolvedTask, None]:
        solved_texts.append(task_text)
        solution = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")
        solved = SolvedTask(
            solution=solution, ref_ok=True, from_cache=False, model="m", prompt_version="v1"
        )
        return solved, None

    monkeypatch.setattr(handlers, "recognize_page_two_stage", fake_recognize)
    monkeypatch.setattr(handlers, "solve_task", fake_solve)
    fake_max = FakeMaxPerUrl()
    store = InMemoryStateStore()
    bot = Bot(
        fake_max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        store,
        EventLog(tmp_path / "events.jsonl", "dev"),
        Settings(_env_file=None),
    )
    return bot, fake_max, store, solved_texts


async def test_album_textbook_plus_notebook_checks_only_notebook_task(harness: Harness) -> None:
    bot, fake_max, store, solved_texts = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    assert fake_max.downloaded == ["https://files/textbook", "https://files/notebook"]
    # солвер вызван один раз — по условию из учебника, а не по семи печатным заданиям
    assert len(solved_texts) == 1
    assert solved_texts[0].startswith("В загородном лагере")
    review = fake_max.sent[-1][1]
    assert "1 из 1 верно" in review
    assert "№19 — верно" in review
    assert "№16" not in review
    state = await store.get(7)
    assert [t.number for t in state.textbook_tasks] == [16, 17, 18, 19, 20, 21, 22]


async def test_textbook_only_is_remembered_and_used_for_next_notebook(harness: Harness) -> None:
    bot, fake_max, _store, solved_texts = harness
    await bot.handle_update(photo_update("textbook"))
    assert solved_texts == []
    assert "учебник" in fake_max.sent[-1][1]
    assert "№16–22" in fake_max.sent[-1][1]
    await bot.handle_update(photo_update("notebook"))
    assert len(solved_texts) == 1
    assert solved_texts[0].startswith("В загородном лагере")
    assert "№19 — верно" in fake_max.sent[-1][1]


async def test_notebook_first_then_textbook_order_independent(harness: Harness) -> None:
    bot, fake_max, _store, solved_texts = harness
    await bot.handle_update(photo_update("notebook", "textbook"))
    assert len(solved_texts) == 1
    assert solved_texts[0].startswith("В загородном лагере")
    assert "№19 — верно" in fake_max.sent[-1][1]


async def test_photo_limit_and_unreadable(harness: Harness) -> None:
    bot, fake_max, _store, _ = harness
    await bot.handle_update(photo_update("blank", "blank", "blank", "blank", "textbook"))
    assert len(fake_max.downloaded) == 4
    assert "Не смог разобрать" in fake_max.sent[-1][1]


async def test_one_failing_photo_does_not_lose_the_others(harness: Harness) -> None:
    bot, fake_max, _store, _ = harness
    await bot.handle_update(photo_update("broken", "textbook", "notebook"))
    review = fake_max.sent[-1][1]
    assert "№19 — верно" in review
    assert "пошло не так" not in review


async def test_all_photos_failing_sends_retry(harness: Harness) -> None:
    bot, fake_max, _store, _ = harness
    await bot.handle_update(photo_update("broken"))
    assert "пошло не так" in fake_max.sent[-1][1]


async def test_stale_textbook_conditions_are_not_reused(harness: Harness) -> None:
    bot, fake_max, store, solved_texts = harness
    await bot.handle_update(photo_update("textbook"))
    state = await store.get(7)
    assert state.textbook_saved_at is not None
    stale = state.model_copy(update={"textbook_saved_at": time.time() - 24 * 3600})
    await store.set(7, stale)
    await bot.handle_update(photo_update("bare"))
    # условия недельной давности не подставляются: без условия солвер не вызывается
    assert solved_texts == []
    assert "№19" in fake_max.sent[-1][1]


async def test_more_than_limit_is_announced(harness: Harness) -> None:
    bot, fake_max, _store, _ = harness
    await bot.handle_update(photo_update("textbook", "notebook", "blank", "blank", "blank"))
    checking = fake_max.sent[-2][1]
    assert "первые 4" in checking


async def test_recognized_page_structure_is_logged(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    bot, _fake_max, _store, _ = harness
    with caplog.at_level(logging.INFO, logger="hwcheck.bot.handlers"):
        await bot.handle_update(photo_update("notebook"))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "role=notebook" in logged
    assert "(19," in logged  # номер задания и флаги: условие/строки/ответ
