"""Сценарий бота «учебник + тетрадь»: альбом из двух фото, учебник отдельным
сообщением, порядок фото, лимит фото. Vision и Solver подменены."""

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot import check
from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.models import MaxUpdate
from hwcheck.config import Settings
from hwcheck.events import EventLog
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import RefSolution, SolvedTask
from hwcheck.pipeline.vision import RecognizedPage
from test_bot import FakeMax
from test_pages import NOTEBOOK_19, TEXTBOOK

PAGES: dict[bytes, VisionPage] = {
    b"textbook": VisionPage(tasks=TEXTBOOK, page_ok=True),
    b"notebook": VisionPage(tasks=NOTEBOOK_19, page_ok=True),
    b"blank": VisionPage(tasks=[], page_ok=False, page_comment="пусто"),
    b"bare": VisionPage(tasks=[NOTEBOOK_19[0].model_copy(update={"task_text": ""})], page_ok=True),
    # живые логи 13.09: ни на одной странице номеров нет, структуризатор пронумеровал с 1
    b"fractions": VisionPage(
        tasks=[
            VisionTask(
                number=1,
                task_text="",
                student_solution_steps=[
                    "4/5 : 9/10 = 4/5 * 10/9 = 40/45 = 8/9",
                    "9/10 : 4/5 = 9/10 * 5/4 = 45/40 = 9/8",
                ],
                confidence=0.9,
            )
        ],
        page_ok=True,
    ),
    b"geometry": VisionPage(
        tasks=[
            VisionTask(number=1, task_text="Отметьте точки K, L и M на луче FE", confidence=0.9),
            VisionTask(number=2, task_text="Проведите прямую SR", confidence=0.9),
        ],
        page_ok=True,
    ),
    # уточняющие вопросы: ответа нет (эталон из учебника есть) и неразборчивый знак
    b"noanswer": VisionPage(
        tasks=[
            VisionTask(
                number=19, task_text="", student_solution_steps=["220 + 180 = 400"], confidence=0.9
            )
        ],
        page_ok=True,
    ),
    b"blurred": VisionPage(
        tasks=[
            VisionTask(
                number=n,
                task_text="",
                student_solution_steps=["15 * 10 + (30 - 20) <неразборчиво> 5 = 200"],
                confidence=0.9,
            )
            for n in (5, 6, 7)
        ],
        page_ok=True,
    ),
}
TRANSCRIPTS: dict[bytes, str] = {
    b"noanswer": "№ 19\n220 + 180 = 400",
    b"blurred": "№ 5\n№ 6\n№ 7\n15 * 10 + (30 - 20) <неразборчиво> 5 = 200",
    b"textbook": "\n".join(f"{n}. условие" for n in range(16, 23)),
    b"notebook": "№ 19\n700 - (220 + 180) = 300",
    b"bare": "№ 19\n700 - (220 + 180) = 300",
    b"fractions": "4/5 : 9/10 = 4/5 * 10/9 = 40/45 = 8/9\n9/10 : 4/5 = 9/10 * 5/4 = 45/40 = 9/8",
    b"geometry": "Отметьте точки K, L и M на луче FE\nПроведите прямую SR",
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
            raw=TRANSCRIPTS.get(image, ""),
        )

    async def fake_solve(_client: Any, task_text: str, **_kw: Any) -> tuple[SolvedTask, None]:
        solved_texts.append(task_text)
        solution = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")
        solved = SolvedTask(
            solution=solution, ref_ok=True, from_cache=False, model="m", prompt_version="v1"
        )
        return solved, None

    monkeypatch.setattr(check, "recognize_page_two_stage", fake_recognize)
    monkeypatch.setattr(check, "solve_task", fake_solve)
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


async def test_synthetic_numbers_do_not_attach_unrelated_textbook_condition(
    harness: Harness,
) -> None:
    # живые логи 13.09: верное деление дробей получило условие «Отметьте точки K, L и M»
    # по совпадению придуманных номеров и ушло в солвер → ложная «ошибка»
    bot, fake_max, _store, solved_texts = harness
    await bot.handle_update(photo_update("fractions", "geometry"))
    assert solved_texts == []
    review = fake_max.sent[-1][1]
    assert "1 из 1 верно" in review
    # номера на странице нет — «№1» путал бы ребёнка
    assert "Задание 1 — верно" in review
    assert "№1" not in review


async def test_textbook_without_numbers_is_described_by_count(harness: Harness) -> None:
    bot, fake_max, _store, _solved = harness
    await bot.handle_update(photo_update("geometry"))
    assert "Вижу страницу учебника (2 задания)" in fake_max.sent[-1][1]


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


# --- шаг 0: причина «не уверен» и статус эталона в событии task_checked ---


def checked_events(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == "task_checked"]


async def test_task_checked_logs_reference_status_ok(harness: Harness, tmp_path: Path) -> None:
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    event = checked_events(tmp_path)[-1]
    assert event["ref_status"] == "ok"
    assert event["reason"] is None
    assert (event["n_steps"], event["n_parsed"], event["has_answer"]) == (1, 1, True)


async def test_task_checked_without_condition(harness: Harness, tmp_path: Path) -> None:
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("bare"))
    assert checked_events(tmp_path)[-1]["ref_status"] == "no_condition"


async def test_task_checked_when_solver_fails(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hwcheck.llm.base import StructuredOutputError

    async def failing_solve(*_args: Any, **_kw: Any) -> tuple[SolvedTask, None]:
        raise StructuredOutputError("bad json")

    monkeypatch.setattr(check, "solve_task", failing_solve)
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    assert checked_events(tmp_path)[-1]["ref_status"] == "solver_failed"


async def test_task_checked_when_reference_not_verified(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unverified_solve(*_args: Any, **_kw: Any) -> tuple[SolvedTask, None]:
        solution = RefSolution(steps=["220 + 180 = 500"], answer="200")
        solved = SolvedTask(
            solution=solution, ref_ok=False, from_cache=False, model="m", prompt_version="v1"
        )
        return solved, None

    monkeypatch.setattr(check, "solve_task", unverified_solve)
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    assert checked_events(tmp_path)[-1]["ref_status"] == "ref_not_verified"


def resolved_events(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == "reference_resolved"]


async def test_reference_resolved_logged_when_solver_verifies(
    harness: Harness, tmp_path: Path
) -> None:
    """Условие есть, солвер сам себя проверил — одно событие reference_resolved, verified."""
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    [event] = resolved_events(tmp_path)
    assert (event["subject"], event["origin"], event["trust"]) == ("math", "derived", "verified")


async def test_reference_resolved_absent_when_solver_unsure(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Солвер не проверил себя (ref_not_verified) — эталона нет (bot/check.py), событие не
    пишется: reference_resolved про уже разрешённый эталон, а не про попытку его получить."""

    async def unverified_solve(*_args: Any, **_kw: Any) -> tuple[SolvedTask, None]:
        solution = RefSolution(steps=["220 + 180 = 500"], answer="200")
        solved = SolvedTask(
            solution=solution, ref_ok=False, from_cache=False, model="m", prompt_version="v1"
        )
        return solved, None

    monkeypatch.setattr(check, "solve_task", unverified_solve)
    bot, _max, _store, _solved = harness
    await bot.handle_update(photo_update("textbook", "notebook"))
    assert resolved_events(tmp_path) == []


# --- уточняющие вопросы (шаг 1) ---


def text_update(text: str, chat_id: int = 7) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": chat_id},
                "body": {"mid": "t", "text": text, "attachments": []},
            },
        }
    )


def callback_update(payload: str, chat_id: int = 7) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_callback",
            "chat_id": chat_id,
            "callback": {"callback_id": "cb", "payload": payload, "user": {"user_id": 42}},
        }
    )


def events_of(tmp_path: Path, kind: str) -> list[dict[str, Any]]:
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == kind]


async def test_missing_answer_is_asked_and_regraded(harness: Harness, tmp_path: Path) -> None:
    bot, fake_max, store, _solved = harness
    await bot.handle_update(photo_update("textbook", "noanswer"))
    review, ask = fake_max.sent[-2][1], fake_max.sent[-1][1]
    assert "№19 — уточню у тебя одну деталь" in review
    assert "не нашёл итоговый ответ" in ask
    assert "300" not in ask  # эталон не раскрываем
    assert (await store.get(7)).phase == "clarifying"

    await bot.handle_update(text_update("300"))
    assert "№19 — верно ✅" in fake_max.sent[-1][1]
    state = await store.get(7)
    assert (state.phase, state.clarifications) == ("review", [])
    answered = events_of(tmp_path, "clarification_answered")[-1]
    assert (answered["verdict_before"], answered["verdict_after"]) == ("uncertain", "correct")
    assert events_of(tmp_path, "task_clarified")[-1]["verdict"] == "correct"
    assert "clarified" not in events_of(tmp_path, "task_checked")[-1]  # не считаем задание дважды


async def test_unreadable_sign_is_chosen_with_buttons(harness: Harness) -> None:
    bot, fake_max, store, _solved = harness
    await bot.handle_update(photo_update("blurred"))
    _chat, ask, buttons = fake_max.sent[-1]
    assert "(30 - 20) ? 5" in ask
    assert buttons is not None
    mul = buttons[0][2]["payload"]
    assert mul.startswith("clarify:") and mul.endswith(":mul")

    await bot.handle_update(callback_update(mul))
    assert "№5 — верно ✅" in fake_max.sent[-2][1]
    second_buttons = fake_max.sent[-1][2]
    assert "(30 - 20) ? 5" in fake_max.sent[-1][1]  # второй вопрос — про №6
    assert second_buttons is not None
    await bot.handle_update(callback_update(second_buttons[0][0]["payload"]))  # «+»
    result_text, result_buttons = fake_max.sent[-1][1], fake_max.sent[-1][2]
    assert "№6 — есть ошибка" in result_text
    assert result_buttons is not None and result_buttons[0][0]["payload"] == "tutor:1"
    state = await store.get(7)
    assert (state.phase, state.clarifications) == ("review", [])


async def test_questions_are_limited_and_others_get_reason(harness: Harness) -> None:
    bot, fake_max, store, _solved = harness
    await bot.handle_update(photo_update("blurred"))
    review = fake_max.sent[-2][1]
    assert review.count("уточню у тебя одну деталь") == 2
    assert "№7 — часть записи неразборчива" in review
    assert "показать взрослому" not in review
    assert len((await store.get(7)).clarifications) == 2


async def test_unclear_reply_twice_leaves_task_as_is(harness: Harness, tmp_path: Path) -> None:
    bot, fake_max, store, _solved = harness
    await bot.handle_update(photo_update("textbook", "noanswer"))
    await bot.handle_update(text_update("не знаю"))
    assert "Напиши только число" in fake_max.sent[-1][1]
    await bot.handle_update(text_update("а что писать"))
    assert "оставлю №19 как есть" in fake_max.sent[-1][1]
    assert (await store.get(7)).phase == "review"
    assert events_of(tmp_path, "clarification_answered")[-1]["understood"] is False


async def test_new_photo_drops_pending_questions(harness: Harness) -> None:
    bot, _fake_max, store, _solved = harness
    await bot.handle_update(photo_update("blurred"))
    await bot.handle_update(photo_update("textbook", "notebook"))
    state = await store.get(7)
    assert (state.phase, state.clarifications) == ("review", [])


async def test_stray_clarify_button_is_harmless(harness: Harness) -> None:
    bot, fake_max, _store, _solved = harness
    await bot.handle_update(callback_update("clarify:mul"))
    assert fake_max.callbacks == ["cb"]
    assert fake_max.sent == []


async def test_stale_sign_button_does_not_answer_next_question(harness: Harness) -> None:
    """Ревью (CRITICAL): повторное нажатие кнопки №5 молча засчитывало №6."""
    bot, fake_max, store, _solved = harness
    await bot.handle_update(photo_update("blurred"))
    first_buttons = fake_max.sent[-1][2]
    assert first_buttons is not None
    mul_for_5 = first_buttons[0][2]["payload"]
    await bot.handle_update(callback_update(mul_for_5))
    sent_before = len(fake_max.sent)

    await bot.handle_update(callback_update(mul_for_5))  # старая кнопка ещё раз
    state = await store.get(7)
    assert state.phase == "clarifying"
    assert [c.task_index for c in state.clarifications] == [1]
    assert state.tasks[1].grade.verdict == "uncertain"
    assert len(fake_max.sent) == sent_before
