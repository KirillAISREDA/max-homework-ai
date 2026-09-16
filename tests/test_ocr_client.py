"""Клиент OCR-сервиса: слова с координатами, таймаут и сбой сети → OcrError, не падение проверки."""

import httpx
import pytest

from hwcheck.ocr_client import OcrClient, OcrError
from hwcheck.subjects.base import Box


def client(handler: httpx.MockTransport) -> OcrClient:
    ocr = OcrClient("http://ocr", timeout_s=1)
    ocr._http = httpx.AsyncClient(base_url="http://ocr", transport=handler)
    return ocr


async def test_recognize_parses_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recognize" and request.content == b"img"
        assert request.headers["content-type"] == "image/jpeg"
        return httpx.Response(
            200,
            json={
                "words": [
                    {"text": "машына", "box": [10, 20, 90, 40], "confidence": 0.4, "line": 0},
                    {"text": "и", "box": None, "confidence": None, "line": 0},
                ],
                "seconds": 5.1,
            },
        )

    words = await client(httpx.MockTransport(handler)).recognize(b"img")
    assert [w.text for w in words] == ["машына", "и"]
    assert words[0].box == Box(x0=10, y0=20, x1=90, y1=40) and words[1].box is None


@pytest.mark.parametrize(
    "outcome", [httpx.Response(500, json={"error": "engine"}), httpx.ReadTimeout("slow")]
)
async def test_failures_become_ocr_error(outcome: httpx.Response | Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(OcrError):
        await client(httpx.MockTransport(handler)).recognize(b"img")


@pytest.mark.parametrize(
    "body",
    [
        {"words": [{"text": "x", "box": [1, 2]}]},  # box короче 4 элементов → IndexError
        {"words": "nope"},  # words не список → ValueError
    ],
)
async def test_malformed_response_becomes_ocr_error(body: dict[str, object]) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    with pytest.raises(OcrError):
        await client(handler).recognize(b"img")


async def test_health() -> None:
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok", "engine": "fake"}))
    assert await client(ok).health()
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    assert not await client(down).health()
