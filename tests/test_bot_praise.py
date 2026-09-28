"""Похвала в сводке: объяснение получают только задания, которые валидатор признал верными;
сбой модели сводку не роняет и не задерживает. Vision и Solver подменены, модель — фейк."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from conftest import FakeLLMClient
from hwcheck.bot import check
from hwcheck.bot import praise as bot_praise
from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask, InMemoryStateStore, RedisStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.praise import explain_correct, with_fallback_praise
from hwcheck.config import Settings
from hwcheck.events import EventLog, anonymize
from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import RefSolution, SolvedTask
from hwcheck.pipeline.vision import RecognizedPage
from hwcheck.subjects.base import Finding, SubjectTask
from hwcheck.subjects.math.module import to_vision_task
from test_bot_pages import FakeMaxPerUrl, photo_update, text_update

WHY = "Ты правильно сложил единицы и не забыл перенести десяток: 803 + 169 = 972."
TECHNIQUE = "сложение столбиком с переходом через десяток"
PRAISE = f"{WHY} Приём — {TECHNIQUE}."
FALLBACK = "Пересчитал твоё действие — сходится."
CAMP = "В загородном лагере за 3 летних месяца отдохнуло 700 ребят. В июне — 220, в июле — 180."


def _task(number: int, steps: list[str], answer: str | None = None, text: str = "") -> VisionTask:
    return VisionTask(
        number=number,
        task_text=text,
        student_solution_steps=steps,
        student_answer=answer,
        confidence=0.9,
    )


CORRECT = _task(17, ["803 + 169 = 972"])
WRONG = _task(18, ["950 + 50 - 660 = 320"])
# условие есть — солвер даёт эталон 300: ни условие, ни эталон не должны уйти в промпт похвалы
WRONG_WITH_REF = _task(19, ["700 - (220 + 180) = 310"], answer="310", text=CAMP)
UNSURE = _task(20, ["Решение: смотри рисунок 45"])
NO_ANSWER = _task(21, ["220 + 180 = 400"], text=CAMP)

PAGES: dict[bytes, VisionPage] = {
    b"correct": VisionPage(tasks=[CORRECT], page_ok=True),
    b"mixed": VisionPage(tasks=[CORRECT, WRONG, WRONG_WITH_REF, UNSURE], page_ok=True),
    b"wrong": VisionPage(tasks=[WRONG, WRONG_WITH_REF], page_ok=True),
    b"noanswer": VisionPage(tasks=[NO_ANSWER], page_ok=True),
}
TRANSCRIPT = "№ 17\n№ 18\n№ 19\n№ 20\n№ 21"

Harness = tuple[Bot, FakeMaxPerUrl, InMemoryStateStore, Path]


def reply(*items: tuple[int, str, str]) -> str:
    rows = [{"index": i, "technique": technique, "why": why} for i, technique, why in items]
    return json.dumps({"items": rows}, ensure_ascii=False)


class BrokenLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        self.calls += 1
        raise ConnectionError("gigachat down")


class SlowLLM(BrokenLLM):
    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        self.calls += 1
        await asyncio.sleep(30)
        raise AssertionError("похвала должна была уложиться в таймаут")


def make_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm: Any) -> Harness:
    async def fake_recognize(_client: Any, image: bytes, **_kw: Any) -> RecognizedPage:
        return RecognizedPage(
            page=PAGES[image],
            orientation=0,
            attempts=0,
            tokens_in=1,
            tokens_out=1,
            latency_s=0.0,
            raw=TRANSCRIPT,
        )

    async def fake_solve(_client: Any, task_text: str, **_kw: Any) -> tuple[SolvedTask, None]:
        solution = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")
        solved = SolvedTask(
            solution=solution, ref_ok=True, from_cache=False, model="m", prompt_version="v1"
        )
        return solved, None

    monkeypatch.setattr(check, "recognize_page_two_stage", fake_recognize)
    monkeypatch.setattr(check, "solve_task", fake_solve)
    fake_max = FakeMaxPerUrl()
    store = InMemoryStateStore()
    events_path = tmp_path / "events.jsonl"
    bot = Bot(fake_max, llm, store, EventLog(events_path, "dev"), Settings(_env_file=None))  # type: ignore[arg-type]
    return bot, fake_max, store, events_path


def events_of(path: Path, kind: str) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == kind]


def prompt_text(client: FakeLLMClient) -> str:
    """Данные, ушедшие модели: системный промпт — файл из репозитория, его не проверяем."""
    return "\n".join(m.content for call in client.calls for m in call if m.role != "system")


# --- всё верно ---


async def test_correct_task_gets_explanation_in_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    bot, fake_max, store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("correct"))

    assert fake_max.sent[-1][1] == f"Проверил! 1 из 1 верно.\n№17 — верно ✅ {PRAISE}"
    assert len(llm.calls) == 1
    # объяснение хранится в состоянии: повторный показ сводки модель не зовёт
    state = await store.get(7)
    assert state.tasks[0].praise == PRAISE
    await bot._send_review(7, state)
    assert fake_max.sent[-1][1] == fake_max.sent[-2][1]
    assert len(llm.calls) == 1


async def test_praise_event_has_metrics_but_no_child_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    bot, _max, _store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("correct"))

    [event] = events_of(events_path, "praise_generated")
    assert event["component"] == "praise"
    assert (event["n_tasks"], event["n_fallback"], event["tokens"]) == (1, 0, 15)
    assert (event["prompt_version"], event["model"]) == ("v1", Settings(_env_file=None).tutor_model)
    assert event["user"] == anonymize(42)
    journal = events_path.read_text(encoding="utf-8")
    assert "сложил" not in journal and "столбиком" not in journal
    assert "803 + 169" not in journal
    assert events_of(events_path, "praise_failed") == []


# --- смесь: верно / ошибка / не уверен ---


async def test_wrong_and_unsure_tasks_never_reach_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    bot, fake_max, _store, _events = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("mixed"))

    sent = prompt_text(llm)
    assert "803 + 169 = 972" in sent
    for secret in ("950", "660", "320", "340", "310", "300", "700", "лагере", "рисунок", "45"):
        assert secret not in sent, secret
    assert fake_max.sent[-1][1] == (
        "Проверил! 1 из 4 верно.\n"
        f"№17 — верно ✅ {PRAISE}\n"
        "№18 — есть ошибка (строка 1) ❌\n"
        "№19 — есть ошибка (строка 1) ❌\n"
        "№20 — не смог разобрать решение 🤔"
    )


async def test_praise_for_a_wrong_task_index_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модель вернула похвалу с индексом ошибочного задания: его строка не меняется."""
    llm = FakeLLMClient(
        [reply((1, "вычитание по частям", "Ты правильно вычел числа."), (0, TECHNIQUE, WHY))]
    )
    bot, fake_max, store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("mixed"))

    lines = fake_max.sent[-1][1].splitlines()
    assert lines[1] == f"№17 — верно ✅ {PRAISE}"
    assert lines[2] == "№18 — есть ошибка (строка 1) ❌"
    state = await store.get(7)
    assert [t.praise for t in state.tasks] == [PRAISE, None, None, None]
    [event] = events_of(events_path, "praise_generated")
    assert (event["n_tasks"], event["n_fallback"]) == (1, 0)


async def test_all_tasks_wrong_means_no_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([])
    bot, fake_max, _store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("wrong"))

    assert llm.calls == []
    assert fake_max.sent[-1][1].startswith("Проверил! 0 из 2 верно.")
    assert events_of(events_path, "praise_generated") == []
    assert events_of(events_path, "praise_failed") == []


# --- защита от выдумок и сбои ---


async def test_invented_number_falls_back_to_validator_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([reply((0, TECHNIQUE, "Ты правильно сложил числа: 803 + 169 = 982."))])
    bot, fake_max, store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("correct"))

    assert fake_max.sent[-1][1] == f"Проверил! 1 из 1 верно.\n№17 — верно ✅ {FALLBACK}"
    assert "982" not in fake_max.sent[-1][1]
    [event] = events_of(events_path, "praise_generated")
    assert (event["n_tasks"], event["n_fallback"]) == (1, 1)


@pytest.mark.parametrize(
    ("llm", "error"),
    [
        (BrokenLLM(), "ConnectionError"),
        (FakeLLMClient(["не json", "снова не json"]), "StructuredOutputError"),
        (None, "AttributeError"),
    ],
)
async def test_model_failure_still_sends_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm: Any, error: str
) -> None:
    bot, fake_max, store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("correct"))

    assert fake_max.sent[-1][1] == f"Проверил! 1 из 1 верно.\n№17 — верно ✅ {FALLBACK}"
    assert (await store.get(7)).tasks[0].praise == FALLBACK
    [failed] = events_of(events_path, "praise_failed")
    assert (failed["component"], failed["error"]) == ("praise", error)
    assert events_of(events_path, "praise_generated") == []
    assert events_of(events_path, "check_failed") == []


async def test_slow_model_is_cut_by_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = SlowLLM()
    monkeypatch.setattr(bot_praise, "PRAISE_TIMEOUT_S", 0.05)
    bot, fake_max, _store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await asyncio.wait_for(bot.handle_update(photo_update("correct")), timeout=5)

    assert fake_max.sent[-1][1] == f"Проверил! 1 из 1 верно.\n№17 — верно ✅ {FALLBACK}"
    assert llm.calls == 1
    [failed] = events_of(events_path, "praise_failed")
    assert failed["error"] == "TimeoutError"


async def test_broken_journal_does_not_break_praise(tmp_path: Path) -> None:
    class BrokenEvents:
        def log(self, *_args: Any, **_kw: Any) -> None:
            raise OSError("disk full")

    item = CheckedTask(task=CORRECT, ref=None, grade=validator_only_grade(["803 + 169 = 972"]))
    llm = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    events: Any = BrokenEvents()

    [explained] = await explain_correct(llm, [item], model="m", events=events, user_id=42)

    assert explained.praise == PRAISE  # сбой журнала не отнимает у ребёнка готовый текст


async def test_bug_in_praise_code_leaves_the_dry_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исключение из шага похвалы ушло бы в общий обработчик проверки и стоило всей сводки."""

    def broken(_grade: Any) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(bot_praise, "fallback_praise", broken)
    llm = FakeLLMClient([])
    bot, fake_max, _store, events_path = make_bot(tmp_path, monkeypatch, llm)

    await bot.handle_update(photo_update("correct"))

    assert fake_max.sent[-1][1] == "Проверил! 1 из 1 верно.\n№17 — верно ✅"
    assert events_of(events_path, "check_failed") == []


# --- что не получает объяснения ---


async def test_tasks_without_steps_or_with_slips_skip_the_model(tmp_path: Path) -> None:
    ref = RefSolution(steps=["700 - 400 = 300"], answer="300")
    answer_only = _task(5, [], answer="300", text=CAMP)
    slip = _task(6, ["220 + 180 = 410", "700 - 400 = 300"], answer="300", text=CAMP)
    items = [
        CheckedTask(
            task=task,
            ref=ref,
            grade=grade(task.student_solution_steps, task.student_answer, ref),
            ref_status="ok",
        )
        for task in (answer_only, slip)
    ]
    llm = FakeLLMClient([])
    events_path = tmp_path / "events.jsonl"

    explained = await explain_correct(
        llm, items, model="m", events=EventLog(events_path, "dev"), user_id=42
    )

    assert llm.calls == []
    assert [t.praise for t in explained] == ["Ответ сошёлся с моим пересчётом."] * 2
    assert not events_path.exists()  # вызова компонента не было — события нет


async def test_language_task_and_stale_findings_are_not_praised(tmp_path: Path) -> None:
    subject_task = SubjectTask(number="245", words=[])
    language = CheckedTask(task=to_vision_task(subject_task), ref=None, subject_task=subject_task)
    # верный пересчёт, но у задания есть подтверждённая находка — оно не «верно»
    finding = Finding(task_index=1, kind="spelling", strength="verified", actual="машына")
    flagged = CheckedTask(
        task=CORRECT,
        ref=None,
        grade=validator_only_grade(["803 + 169 = 972"]),
        findings=[finding],
    )
    llm = FakeLLMClient([])

    explained = await explain_correct(
        llm,
        [language, flagged],
        model="m",
        events=EventLog(tmp_path / "events.jsonl", "dev"),
        user_id=42,
    )

    assert llm.calls == []
    assert explained == [language, flagged]
    assert with_fallback_praise(0, language) == language
    assert with_fallback_praise(1, flagged).praise is None


async def test_grade_of_the_student_reaches_the_prompt(tmp_path: Path) -> None:
    item = CheckedTask(task=CORRECT, ref=None, grade=validator_only_grade(["803 + 169 = 972"]))
    llm = FakeLLMClient([reply((0, TECHNIQUE, WHY))])

    await explain_correct(
        llm,
        [item],
        model="m",
        events=EventLog(tmp_path / "events.jsonl", "dev"),
        user_id=42,
        grade=3,
    )

    assert "Класс ученика: 3" in prompt_text(llm)


# --- уточняющий вопрос: задание стало «верно» после ответа ученика ---


async def test_clarified_task_gets_fallback_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLMClient([])
    bot, fake_max, store, events_path = make_bot(tmp_path, monkeypatch, llm)
    await bot.handle_update(photo_update("noanswer"))
    assert "№21 — уточню у тебя одну деталь" in fake_max.sent[-2][1]

    await bot.handle_update(text_update("300"))

    clarified = "Пересчитал твоё действие — сходится, и ответ верный."
    assert fake_max.sent[-1][1] == f"№21 — верно ✅ {clarified}"
    assert llm.calls == []
    assert (await store.get(7)).tasks[0].praise == clarified
    assert events_of(events_path, "task_clarified")[-1]["verdict"] == "correct"


# --- старые состояния Redis ---


async def test_redis_state_without_praise_field_is_read() -> None:
    item = CheckedTask(task=CORRECT, ref=None, grade=validator_only_grade(["803 + 169 = 972"]))
    state = ChatState(phase="review", tasks=[item])
    old = json.loads(state.model_dump_json())
    for task in old["tasks"]:
        del task["praise"]  # запись до появления поля
    client = fakeredis.FakeAsyncRedis()
    await client.set(f"fsm:{anonymize(7)}", json.dumps(old))

    restored = await RedisStateStore(client).get(7)

    assert restored.phase == "review"
    assert restored.tasks[0].praise is None
    assert restored.tasks[0].task.number == 17
