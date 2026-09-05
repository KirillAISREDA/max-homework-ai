"""Пересланные и отвеченные сообщения: MAX кладёт вложения в message.link.message,
а body самого сообщения приходит пустым (живой тест, сессия 9). Бот должен видеть
фото и там, иначе «Переслать фото боту» молча игнорируется."""

from hwcheck.bot.models import MaxUpdate

FORWARDED_PHOTO = {
    "update_type": "message_created",
    "timestamp": 1788533180845,
    "user_locale": "ru",
    "message": {
        "recipient": {"chat_type": "dialog", "chat_id": 428933823, "user_id": 1},
        "sender": {"user_id": 1, "name": "U", "is_bot": False},
        "timestamp": 1788533180845,
        "body": {"mid": "m1", "seq": 1, "text": ""},
        "link": {
            "type": "forward",
            "chat_id": 777,
            "sender": {"user_id": 2, "name": "V", "is_bot": False},
            "message": {
                "mid": "m0",
                "seq": 0,
                "text": "",
                "attachments": [
                    {
                        "type": "image",
                        "payload": {
                            "photo_id": 40322490461,
                            "token": "t",
                            "url": "https://i.oneme.ru/forwarded.jpg",
                        },
                    }
                ],
            },
        },
    },
}


def test_forwarded_photo_is_visible() -> None:
    update = MaxUpdate.model_validate(FORWARDED_PHOTO)
    assert update.message is not None
    assert update.message.image_urls == ["https://i.oneme.ru/forwarded.jpg"]


def test_reply_to_photo_is_visible_too() -> None:
    data = {**FORWARDED_PHOTO, "message": {**FORWARDED_PHOTO["message"]}}
    data["message"]["link"] = {**FORWARDED_PHOTO["message"]["link"], "type": "reply"}
    update = MaxUpdate.model_validate(data)
    assert update.message is not None
    assert update.message.image_urls == ["https://i.oneme.ru/forwarded.jpg"]


def test_own_attachments_come_before_linked() -> None:
    data = {**FORWARDED_PHOTO, "message": {**FORWARDED_PHOTO["message"]}}
    data["message"]["body"] = {
        "mid": "m1",
        "text": "",
        "attachments": [{"type": "image", "payload": {"url": "https://i.oneme.ru/own.jpg"}}],
    }
    update = MaxUpdate.model_validate(data)
    assert update.message is not None
    assert update.message.image_urls == [
        "https://i.oneme.ru/own.jpg",
        "https://i.oneme.ru/forwarded.jpg",
    ]


def test_link_without_message_is_harmless() -> None:
    message = {**FORWARDED_PHOTO["message"], "link": {"type": "forward"}}
    data = {**FORWARDED_PHOTO, "message": message}
    update = MaxUpdate.model_validate(data)
    assert update.message is not None
    assert update.message.image_urls == []
