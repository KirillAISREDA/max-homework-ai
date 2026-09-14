"""Шифрование id MAX (152-ФЗ, спецификация онбординга §10.1): в базе — только шифротекст.

Для поиска пользователя — HMAC-хэш (`events.anonymize`), для отправки сообщения — расшифровка.
Ключ — `USER_ID_KEY` (Fernet). Без него бот не сможет писать пользователям: копия ключа хранится
вне VPS.
"""

from cryptography.fernet import Fernet, InvalidToken


class UserIdCipherError(ValueError):
    pass


class UserIdCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise UserIdCipherError(
                "USER_ID_KEY: нужен ключ Fernet (новый — python -m hwcheck keys)"
            ) from exc

    def encrypt(self, user_id: int) -> bytes:
        return self._fernet.encrypt(str(user_id).encode())

    def decrypt(self, token: bytes) -> int:
        try:
            return int(self._fernet.decrypt(token).decode())
        except InvalidToken as exc:
            raise UserIdCipherError("id не расшифровывается этим USER_ID_KEY") from exc


def new_user_id_key() -> str:
    return Fernet.generate_key().decode()
