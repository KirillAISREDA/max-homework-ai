"""Загрузка изображения в MAX (POST /uploads → файл → токен) и сообщение с картинкой."""

import json

import httpx

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
