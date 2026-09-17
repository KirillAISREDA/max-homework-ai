"""Ответ на нажатие кнопки (POST /answers).

Живьём (17.09, онбординг): MAX отвечает 400 на пустое тело `{}` — нужен `message` или
`notification`. Кнопки математики слали `notification` и работали, онбординг и уточняющие
вопросы — пустое тело и падали целиком (ученик видел «попробуй ещё раз»).
"""

import json
import logging

import httpx
import pytest

from hwcheck.bot.max_api import MaxClient


def _client(handler) -> MaxClient:  # type: ignore[no-untyped-def]
    client = MaxClient("token")
    client._http = httpx.AsyncClient(
        base_url="https://max.test", transport=httpx.MockTransport(handler)
    )
    return client


async def test_answer_without_notification_sends_empty_notification() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    async with _client(handler) as client:
        await client.answer_callback("cb1")
        await client.answer_callback("cb2", notification="Разбираем №5")

    assert requests[0].url.params["callback_id"] == "cb1"
    assert json.loads(requests[0].content) == {"notification": ""}
    assert json.loads(requests[1].content) == {"notification": "Разбираем №5"}


async def test_answer_4xx_is_logged_with_body_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # тело ответа может эхом содержать callback_id — в лог он попасть не должен
        return httpx.Response(
            400, json={"code": "proto.payload", "message": "callback_id: cb1 is expired"}
        )

    with caplog.at_level(logging.WARNING, logger="hwcheck.bot.max_api"):
        async with _client(handler) as client:
            # ack не критичен: кнопка «крутится», но сценарий идёт
            await client.answer_callback("cb1")

    assert "HTTP 400" in caplog.text and "is expired" in caplog.text
    assert "cb1" not in caplog.text  # callback_id — одноразовый секрет кнопки, в лог не пишем
