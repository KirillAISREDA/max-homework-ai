"""Журнал вызовов модели: одно событие `llm_call` на каждый вызов, в том числе упавший."""

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import Answer
from hwcheck.events import EventLog, anonymize, trace
from hwcheck.llm.base import ChatMessage, LLMResult, StructuredOutputError, chat_structured
from hwcheck.llm.journal import JournaledLLM, llm_step
from hwcheck.pipeline.solver import FileCache, solve_task

SECRET_PROMPT = "Маша решила пример 2+2=5"
SECRET_ANSWER = "ответ модели про Машу"
GOOD_REF = json.dumps({"steps": ["430/5 = 86"], "answer": "86", "units": None})


class FakeInner:
    """Клиент с chat и vision: отдаёт заданные ответы, исключение в списке — бросает."""

    def __init__(self, responses: Sequence[str | BaseException]) -> None:
        self._responses = list(responses)

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        return self._next(model, tokens_in=120, tokens_out=30)

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        return self._next(model, tokens_in=1800, tokens_out=200)

    def _next(self, model: str, *, tokens_in: int, tokens_out: int) -> LLMResult:
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return LLMResult(content=response, model=model, tokens_in=tokens_in, tokens_out=tokens_out)


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def llm_calls(path: Path) -> list[dict[str, Any]]:
    return [e for e in read_events(path) if e["type"] == "llm_call"]


def user_message(text: str = SECRET_PROMPT) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


async def test_chat_writes_one_event_with_all_fields(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner([SECRET_ANSWER]), EventLog(path, "prod"))

    with llm_step("solver", "v1"):
        result = await client.chat(user_message(), model="GigaChat-2-Max")

    assert result.content == SECRET_ANSWER  # ответ внутреннего клиента отдан как есть
    [event] = llm_calls(path)
    assert event["component"] == "llm"
    assert event["user_initiated"] is False
    assert (event["step"], event["prompt_version"]) == ("solver", "v1")
    assert (event["model"], event["kind"]) == ("GigaChat-2-Max", "chat")
    assert (event["tokens_in"], event["tokens_out"]) == (120, 30)
    assert isinstance(event["latency_ms"], int) and event["latency_ms"] >= 0
    assert (event["status"], event["error"]) == ("ok", None)


async def test_vision_call_is_marked_as_vision(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner(["транскрипция"]), EventLog(path, "prod"))

    with llm_step("vision", "v3"):
        await client.analyze_image(b"img", prompt=SECRET_PROMPT, model="GigaChat-2-Max")

    [event] = llm_calls(path)
    assert (event["kind"], event["step"], event["prompt_version"]) == ("vision", "vision", "v3")
    assert (event["tokens_in"], event["tokens_out"]) == (1800, 200)


async def test_prompt_and_answer_are_not_written(tmp_path: Path) -> None:
    """152-ФЗ: в промпте и ответе — текст тетради ребёнка, в журнал он не попадает."""
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner([SECRET_ANSWER, SECRET_ANSWER]), EventLog(path, "prod"))

    await client.chat(user_message(), model="m")
    await client.analyze_image(b"img", prompt=SECRET_PROMPT, model="m", filename="маша.jpg")

    written = path.read_text(encoding="utf-8")
    assert "Маш" not in written and "маша" not in written
    assert len(llm_calls(path)) == 2


async def test_failed_call_is_logged_and_exception_passes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    failure = TimeoutError("api down")
    client = JournaledLLM(FakeInner([failure]), EventLog(path, "prod"))

    with llm_step("tutor", "v1"), pytest.raises(TimeoutError) as raised:
        await client.chat(user_message(), model="GigaChat-2-Pro")

    assert raised.value is failure
    [event] = llm_calls(path)
    assert (event["status"], event["error"]) == ("error", "TimeoutError")
    assert (event["tokens_in"], event["tokens_out"]) == (0, 0)
    assert (event["step"], event["model"]) == ("tutor", "GigaChat-2-Pro")
    assert "api down" not in path.read_text(encoding="utf-8")  # текст ошибки — не в журнал


async def test_failed_vision_call_is_logged(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner([RuntimeError("429")]), EventLog(path, "prod"))

    with pytest.raises(RuntimeError):
        await client.analyze_image(b"img", prompt="p", model="GigaChat-2-Max")

    [event] = llm_calls(path)
    assert (event["kind"], event["status"], event["error"]) == ("vision", "error", "RuntimeError")


async def test_call_outside_step_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner(["a", "b"]), EventLog(path, "prod"))

    with llm_step("solver", "v1"):
        await client.chat(user_message(), model="m")
    await client.chat(user_message(), model="m")  # контекст шага закрыт

    steps = [(e["step"], e["prompt_version"]) for e in llm_calls(path)]
    assert steps == [("solver", "v1"), ("unknown", None)]


async def test_nested_step_restores_outer(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner(["a", "b", "c"]), EventLog(path, "prod"))

    with llm_step("vision", "v3"):
        await client.chat(user_message(), model="m")
        with llm_step("vision_structure", "v1"):
            await client.chat(user_message(), model="m")
        await client.chat(user_message(), model="m")

    assert [e["step"] for e in llm_calls(path)] == ["vision", "vision_structure", "vision"]


async def test_event_gets_user_env_and_trace_of_update(tmp_path: Path) -> None:
    """Антифрод: вызов модели по апдейту тестера — env=test, пользователь обезличен."""
    path = tmp_path / "events.jsonl"
    tester = anonymize(42)
    assert tester is not None
    log = EventLog(path, "prod", test_users={tester})
    client = JournaledLLM(FakeInner(["a", "b", "c"]), log)

    with trace(user_id=42) as tester_trace:
        await client.chat(user_message(), model="m")
    with trace(user_id=43) as user_trace:
        await client.chat(user_message(), model="m")
    await client.chat(user_message(), model="m")  # вне апдейта

    events = llm_calls(path)
    assert [e["env"] for e in events] == ["test", "prod", "prod"]
    assert [e["user"] for e in events] == [tester, anonymize(43), None]
    assert [e["trace_id"] for e in events] == [tester_trace, user_trace, None]
    assert "42" not in {e["user"] for e in events}


async def test_structured_retry_is_two_events_of_the_same_step(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    valid = json.dumps({"value": 1, "comment": "ok"})
    client = JournaledLLM(FakeInner(["не json", valid]), EventLog(path, "prod"))

    with llm_step("classifier", "v1"):
        answer, _ = await chat_structured(client, user_message(), Answer, model="m")

    assert answer.value == 1
    events = llm_calls(path)
    assert [(e["step"], e["status"]) for e in events] == [("classifier", "ok")] * 2


async def test_structured_failure_after_retry_keeps_both_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    client = JournaledLLM(FakeInner(["не json", "снова не json"]), EventLog(path, "prod"))

    with llm_step("solver", "v1"), pytest.raises(StructuredOutputError):
        await chat_structured(client, user_message(), Answer, model="m")

    # модель ответила оба раза — вызовы состоялись и оплачены, хотя шаг не удался
    assert [e["status"] for e in llm_calls(path)] == ["ok", "ok"]


async def test_solver_cache_hit_writes_no_event(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    cache = FileCache(tmp_path / "cache")
    client = JournaledLLM(FakeInner([GOOD_REF]), EventLog(path, "prod"))

    first, _ = await solve_task(client, "430 км за 5 ч", model="GigaChat-2-Max", cache=cache)
    assert first.from_cache is False
    assert [e["step"] for e in llm_calls(path)] == ["solver"]

    second, result = await solve_task(client, "430 км за 5 ч", model="GigaChat-2-Max", cache=cache)
    assert second.from_cache is True and result is None
    assert len(llm_calls(path)) == 1  # вызова модели не было — события нет


async def test_journal_write_failure_does_not_break_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # путь журнала — каталог: запись падает с OSError, как при переполненном диске
    broken = EventLog(tmp_path, "prod")
    client = JournaledLLM(FakeInner(["ответ"]), broken)

    with caplog.at_level(logging.WARNING, logger="hwcheck.llm.journal"):
        result = await client.chat(user_message(), model="m")

    assert result.content == "ответ"
    assert any("llm_call" in record.getMessage() for record in caplog.records)


async def test_journal_write_failure_does_not_hide_the_model_error(tmp_path: Path) -> None:
    failure = TimeoutError("api down")
    client = JournaledLLM(FakeInner([failure]), EventLog(tmp_path, "prod"))

    with pytest.raises(TimeoutError) as raised:
        await client.chat(user_message(), model="m")

    assert raised.value is failure
