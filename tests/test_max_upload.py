"""Загрузка изображения в MAX (POST /uploads → файл → токен) и сообщение с картинкой."""

import json

import httpx
import pytest

from hwcheck.bot.max_api import MaxClient


async def test_upload_image_and_send_with_attachment() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/uploads":
            assert request.url.params["type"] == "image"
            return httpx.Response(200, json={"url": "https://iu.test/upload/abc", "token": "t0"})
        if request.url.host == "iu.test":
            assert b'name="data"' in request.content and b"jpegbytes" in request.content
            return httpx.Response(200, json={"token": "tok123"})
        return httpx.Response(200, json={})

    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        transport = httpx.MockTransport(handler)
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=transport)
        client._files = httpx.AsyncClient(transport=transport)
        token = await client.upload_image(b"jpegbytes")
        await client.send_message(7, "здесь написано «машына»?", image_token=token)

    assert token == "tok123"
    body = json.loads(requests[-1].content)
    assert body["attachments"] == [{"type": "image", "payload": {"token": "tok123"}}]
    assert "Authorization" not in requests[1].headers  # файл — на сторонний хост без токена бота


@pytest.mark.parametrize(
    "body",
    [
        {"token": "tok123"},  # по документации dev.max.ru
        {"photos": {"g1/abc==": {"token": "tok123"}}},  # живой ответ iu.oneme.ru, 17.09
    ],
)
async def test_upload_accepts_both_response_shapes(body: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://iu.test/upload/abc"})
        return httpx.Response(200, json=body)

    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        transport = httpx.MockTransport(handler)
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=transport)
        client._files = httpx.AsyncClient(transport=transport)
        assert await client.upload_image(b"jpegbytes") == "tok123"


async def test_upload_without_token_is_an_error() -> None:
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={"url": "https://iu.test/u"} if r.url.path == "/uploads" else {"photos": {}},
        )
    )
    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=handler)
        client._files = httpx.AsyncClient(transport=handler)
        with pytest.raises(ValueError, match="token"):
            await client.upload_image(b"jpegbytes")


async def test_upload_refuses_plain_http_address() -> None:
    """Адрес загрузки приходит из ответа MAX: фото домашки уходит только по https."""
    handler = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"url": "http://iu.test/upload/abc"})
    )
    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=handler)
        client._files = httpx.AsyncClient(transport=handler)
        with pytest.raises(ValueError, match="https"):
            await client.upload_image(b"jpegbytes")


async def test_upload_does_not_follow_redirect() -> None:
    """Редирект не пересылает фото на другой хост молча — это ошибка, а не вторая отправка."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://iu.test/upload/abc"})
        return httpx.Response(307, headers={"Location": "https://evil.test/take"})

    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        transport = httpx.MockTransport(handler)
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=transport)
        client._files = httpx.AsyncClient(transport=transport, follow_redirects=True)
        with pytest.raises(httpx.HTTPStatusError):
            await client.upload_image(b"jpegbytes")

    assert [r.url.host for r in requests] == ["max.test", "iu.test"]  # evil.test не позвали
