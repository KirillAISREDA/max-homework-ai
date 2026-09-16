"""Клиент MAX: сообщение пользователю по id (вторая сторона связки, спецификация §4.2) и метка
`bot_started` (deep link `?start=`, §4.4)."""

import json

import httpx

from hwcheck.bot.max_api import MaxClient, callback_button
from hwcheck.bot.models import MaxUpdate


async def test_send_to_user_uses_user_id_and_send_message_chat_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with MaxClient("token") as client:
        await client._http.aclose()
        client._http = httpx.AsyncClient(
            base_url="https://max.test", transport=httpx.MockTransport(handler)
        )
        await client.send_to_user(42, "привет", buttons=[[callback_button("Да", "ob:accept")]])
        await client.send_message(7, "в чат")

    assert [dict(r.url.params) for r in requests] == [{"user_id": "42"}, {"chat_id": "7"}]
    body = json.loads(requests[0].content)
    assert body["text"] == "привет"
    assert body["attachments"][0]["payload"]["buttons"][0][0]["payload"] == "ob:accept"


def test_bot_started_payload_is_parsed() -> None:
    update = MaxUpdate.model_validate(
        {
            "update_type": "bot_started",
            "chat_id": 70,
            "user": {"user_id": 7},
            "payload": "p_abcdefghijklmnopqrstuv",
            "user_locale": "ru",
        }
    )
    assert update.payload == "p_abcdefghijklmnopqrstuv"
    assert (update.effective_chat_id, update.effective_user_id) == (70, 7)
    assert MaxUpdate.model_validate({"update_type": "bot_started"}).payload is None
