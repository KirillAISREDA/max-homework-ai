"""Клиент шлюза моделей (OpenAI-совместимый API): запрос, ответ, стоимость, повторы, ошибки."""

import base64
import json
from typing import Any

import httpx
import pytest

from hwcheck.llm.base import ChatMessage
from hwcheck.llm.gateway_client import GatewayClient, GatewayError

BASE = "https://gateway.test/v1"
MESSAGES = [ChatMessage(role="user", content="тест")]


def answer(
    content: str = "привет", *, cost: str | None = "0.25", status: int = 200
) -> httpx.Response:
    headers = {"x-litellm-response-cost": cost} if cost is not None else {}
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    return httpx.Response(status, json=body, headers=headers)


class Gateway:
    """Шлюз-заглушка: отдаёт ответы по очереди и запоминает запросы."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def make_client(gateway: Gateway, **kwargs: Any) -> GatewayClient:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = GatewayClient(
        BASE, "sk-test", transport=httpx.MockTransport(gateway), sleep=sleep, **kwargs
    )
    client.delays = delays  # type: ignore[attr-defined]
    return client


async def test_chat_sends_openai_request_and_reads_usage_and_cost() -> None:
    gateway = Gateway(answer())
    async with make_client(gateway) as client:
        result = await client.chat(MESSAGES, model="gigachat-2-max", temperature=0.3)

    request = gateway.requests[0]
    assert str(request.url) == f"{BASE}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert gateway.body() == {
        "model": "gigachat-2-max",
        "messages": [{"role": "user", "content": "тест"}],
        "temperature": 0.3,
    }
    assert result.content == "привет"
    assert (result.model, result.tokens_in, result.tokens_out) == ("gigachat-2-max", 100, 50)
    assert result.cost == pytest.approx(0.25)
    assert result.latency_s >= 0


async def test_image_goes_inline_as_data_url() -> None:
    gateway = Gateway(answer("803 + 169 = 972"))
    async with make_client(gateway) as client:
        result = await client.analyze_image(
            b"\x89PNG-bytes", prompt="Перепиши страницу", model="vision-model", filename="page.png"
        )

    [message] = gateway.body()["messages"]
    text, image = message["content"]
    assert text == {"type": "text", "text": "Перепиши страницу"}
    encoded = base64.b64encode(b"\x89PNG-bytes").decode()
    assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
    assert result.content == "803 + 169 = 972"


async def test_answer_without_cost_and_usage_is_still_an_answer() -> None:
    bare = httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    async with make_client(Gateway(bare)) as client:
        result = await client.chat(MESSAGES, model="m")
    assert (result.content, result.tokens_in, result.tokens_out, result.cost) == ("", 0, 0, None)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_overloaded_gateway_is_retried_with_growing_pause(status: int) -> None:
    busy = httpx.Response(status, json={"error": {"message": "busy"}})
    gateway = Gateway(busy, busy, answer("готово"))
    async with make_client(gateway, max_retries=3) as client:
        result = await client.chat(MESSAGES, model="m")
    assert result.content == "готово"
    assert len(gateway.requests) == 3
    assert client.delays == [1.0, 2.0]  # type: ignore[attr-defined]


async def test_network_failure_is_retried() -> None:
    gateway = Gateway(httpx.ConnectError("нет связи"), answer("готово"))
    async with make_client(gateway) as client:
        assert (await client.chat(MESSAGES, model="m")).content == "готово"


async def test_retries_run_out_with_the_last_error() -> None:
    busy = httpx.Response(503, json={"error": {"message": "busy"}})
    gateway = Gateway(busy, busy, busy)
    async with make_client(gateway, max_retries=2) as client:
        with pytest.raises(GatewayError) as caught:
            await client.chat(MESSAGES, model="m")
    assert caught.value.status_code == 503
    assert len(gateway.requests) == 3


async def test_bad_request_is_not_retried_and_keeps_the_reason() -> None:
    """Ошибка запроса повтором не лечится: модель не читает изображения, ключ не тот."""
    refused = httpx.Response(400, json={"error": {"message": "is not a multimodal model"}})
    gateway = Gateway(refused)
    async with make_client(gateway) as client:
        with pytest.raises(GatewayError, match="not a multimodal") as caught:
            await client.chat(MESSAGES, model="m")
    assert caught.value.status_code == 400
    assert len(gateway.requests) == 1


async def test_error_text_never_carries_the_key() -> None:
    leaked = httpx.Response(401, json={"error": {"message": "bad key sk-test, попробуйте ещё"}})
    async with make_client(Gateway(leaked)) as client:
        with pytest.raises(GatewayError) as caught:
            await client.chat(MESSAGES, model="m")
    assert "sk-test" not in str(caught.value)


async def test_answer_without_choices_is_an_error_not_an_empty_text() -> None:
    broken = httpx.Response(200, json={"usage": {"prompt_tokens": 5}})
    async with make_client(Gateway(broken)) as client:
        with pytest.raises(GatewayError, match="без ответа модели"):
            await client.chat(MESSAGES, model="m")


def test_client_needs_address_and_key() -> None:
    with pytest.raises(ValueError, match="LLM_GATEWAY_URL"):
        GatewayClient("", "sk-test")
    with pytest.raises(ValueError, match="LLM_GATEWAY_KEY"):
        GatewayClient(BASE, "")
