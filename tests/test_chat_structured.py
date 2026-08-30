import pytest

from conftest import Answer, FakeLLMClient
from hwcheck.llm.base import ChatMessage, StructuredOutputError, chat_structured, extract_json

MESSAGES = [ChatMessage(role="user", content="сколько будет 2+2?")]


async def test_valid_json_first_try() -> None:
    client = FakeLLMClient(['{"value": 4, "comment": "ok"}'])
    answer, result = await chat_structured(client, MESSAGES, Answer, model="test")
    assert answer.value == 4
    assert len(client.calls) == 1
    assert result.tokens_in == 10


async def test_json_in_markdown_fence() -> None:
    client = FakeLLMClient(['Вот ответ:\n```json\n{"value": 4, "comment": "ok"}\n```'])
    answer, _ = await chat_structured(client, MESSAGES, Answer, model="test")
    assert answer.value == 4


async def test_retry_once_on_invalid_json() -> None:
    client = FakeLLMClient(["не json", '{"value": 4, "comment": "после retry"}'])
    answer, result = await chat_structured(client, MESSAGES, Answer, model="test")
    assert answer.comment == "после retry"
    assert len(client.calls) == 2
    # retry-сообщение содержит указание на невалидность и историю
    assert "валидацию" in client.calls[1][-1].content
    # токены обоих вызовов суммируются для учёта стоимости
    assert result.tokens_in == 20


async def test_error_after_failed_retry() -> None:
    client = FakeLLMClient(["не json", "снова не json"])
    with pytest.raises(StructuredOutputError):
        await chat_structured(client, MESSAGES, Answer, model="test")
    assert len(client.calls) == 2


def test_extract_json_passthrough() -> None:
    assert extract_json('  {"a": 1}  ') == '{"a": 1}'
    assert extract_json('```\n{"a": 1}\n```') == '{"a": 1}'
