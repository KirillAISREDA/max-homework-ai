import json

import pytest
from pydantic import BaseModel

from conftest import Answer, FakeLLMClient
from hwcheck.llm.base import (
    ChatMessage,
    StructuredOutputError,
    chat_structured,
    close_brackets,
    extract_json,
    repair_json,
)

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


async def test_lost_closing_bracket_costs_no_retry() -> None:
    """GigaChat теряет последнюю «}» (живая проба 28.09): ответ полный, повторный вызов только
    удваивает токены и время."""
    client = FakeLLMClient(['{"value": 4, "comment": "скобка } в тексте"'])
    answer, _ = await chat_structured(client, MESSAGES, Answer, model="test")
    assert (answer.value, answer.comment) == (4, "скобка } в тексте")
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("broken", "closed"),
    [
        ('{"items": [{"a": 1}, {"a": 2}]', '{"items": [{"a": 1}, {"a": 2}]}'),
        ('{"items": [{"a": "x]"}', '{"items": [{"a": "x]"}]}'),
        ('{"a": "кавычка \\" и {"', '{"a": "кавычка \\" и {"}'),
        ('{"a": 1}', '{"a": 1}'),  # целый ответ не меняется
    ],
)
def test_close_brackets(broken: str, closed: str) -> None:
    assert close_brackets(broken) == closed


@pytest.mark.parametrize("text", ['{"a": "оборвано на стро', '{"a": 1}}', '{"a": [1}', "не json"])
def test_close_brackets_does_not_guess(text: str) -> None:
    """Оборванная строка, лишняя или чужая скобка — не потерянный хвост: текст остаётся как был."""
    assert close_brackets(text) == text


class Item(BaseModel):
    index: int
    text: str


class Items(BaseModel):
    items: list[Item]


GLUED = (
    '{"items": [{"index": 0, "text": "первое", "index": 1, "text": "второе", '
    '"index": 2, "text": "третье"}]'
)


async def test_objects_glued_in_a_list_are_split() -> None:
    """GigaChat теряет «}, {» между объектами списка (живая проба 28.09): ключи повторяются,
    обычный разбор молча оставляет последний объект — два объяснения из трёх пропадали."""
    client = FakeLLMClient([GLUED])
    answer, _ = await chat_structured(client, MESSAGES, Items, model="test")
    assert [(i.index, i.text) for i in answer.items] == [
        (0, "первое"),
        (1, "второе"),
        (2, "третье"),
    ]
    assert len(client.calls) == 1


def test_repair_leaves_valid_json_untouched() -> None:
    valid = '{"items": [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}], "n": 2.50}'
    assert repair_json(valid) == valid


def test_repair_splits_glued_objects_only_inside_lists() -> None:
    # повтор ключа вне списка делить не на что: остаётся обычное правило «последний побеждает»
    assert json.loads(repair_json('{"a": 1, "a": 2}')) == {"a": 2}
    nested = '{"pages": [{"tasks": [{"n": 1, "n": 2}], "role": "x", "role": "y"}]}'
    # внутренний список расклеен; у внешнего объекта куски с разными полями — не трогаем
    assert json.loads(repair_json(nested)) == {
        "pages": [{"tasks": [{"n": 1}, {"n": 2}], "role": "y"}]
    }


def test_repair_does_not_touch_what_is_not_json() -> None:
    assert repair_json("не json") == "не json"


def test_glued_objects_with_different_fields_are_not_split() -> None:
    """Куски с разным набором полей — не потерянное «}, {», а перестановка: расклейка приписала
    бы ответ одного задания другому (ревью 28.09). Такой ответ остаётся как был."""
    stolen = (
        '{"tasks": [{"number": 1, "text": "2+2", "answer": "6", "number": 2, "text": "3+3"}], '
        '"ok": true}'
    )
    assert repair_json(stolen) == stolen
    assert json.loads(repair_json(stolen)) == {
        "tasks": [{"number": 2, "text": "3+3", "answer": "6"}],
        "ok": True,
    }


def test_repair_survives_hopelessly_nested_answer() -> None:
    # модель зациклилась на скобках: починка не должна падать раньше обычной проверки схемы
    nested = "[" * 5000 + "1" + "]" * 5000
    assert repair_json(nested) == nested
