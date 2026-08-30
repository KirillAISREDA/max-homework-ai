from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from hwcheck.config import Settings
from hwcheck.llm.base import ChatMessage
from hwcheck.llm.gigachat_client import GigaChatClient


def make_client() -> GigaChatClient:
    return GigaChatClient(Settings(gigachat_credentials="test", _env_file=None))


def fake_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )


async def test_chat_maps_payload_and_usage() -> None:
    client = make_client()
    achat = AsyncMock(return_value=fake_response("привет"))
    client._client.achat = achat  # type: ignore[method-assign]

    result = await client.chat(
        [ChatMessage(role="user", content="тест")], model="GigaChat-2", temperature=0.3
    )

    payload: dict[str, Any] = achat.call_args.args[0]
    assert payload["model"] == "GigaChat-2"
    assert payload["messages"] == [{"role": "user", "content": "тест"}]
    assert payload["temperature"] == 0.3
    assert result.content == "привет"
    assert (result.tokens_in, result.tokens_out) == (100, 50)
    assert result.latency_s > 0


async def test_analyze_image_uploads_and_attaches() -> None:
    client = make_client()
    client._client.aupload_file = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id_="file-123")
    )
    achat = AsyncMock(return_value=fake_response('{"tasks": []}'))
    client._client.achat = achat  # type: ignore[method-assign]

    result = await client.analyze_image(
        b"fake-image", prompt="распознай", model="GigaChat-2-Max", filename="photo.png"
    )

    upload_arg = client._client.aupload_file.call_args.args[0]
    assert upload_arg == ("photo.png", b"fake-image", "image/png")
    payload: dict[str, Any] = achat.call_args.args[0]
    assert payload["messages"][0]["attachments"] == ["file-123"]
    assert result.content == '{"tasks": []}'
