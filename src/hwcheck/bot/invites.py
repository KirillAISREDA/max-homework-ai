"""Приглашения «ребёнок ↔ родитель» (спецификация онбординга §4.4, §7): ссылка и запасной код.

Ссылка `https://max.ru/<бот>?start=p_<токен>` (ребёнок зовёт родителя) или `c_<токен>` (родитель
зовёт ребёнка); запасной код — 8 знаков без похожих 0/O и 1/I. В базе — только sha256 токена и
кода: утечка таблицы не даёт погасить чужое приглашение.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Literal

InviteKind = Literal["student_invites_parent", "parent_invites_student"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
INVITE_TTL_DAYS = 7
MAX_START_PAYLOAD = 128  # лимит MAX на метку ?start=

_PREFIX: dict[InviteKind, str] = {"student_invites_parent": "p", "parent_invites_student": "c"}
_KIND_BY_PREFIX: dict[str, InviteKind] = {prefix: kind for kind, prefix in _PREFIX.items()}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CODE = re.compile(rf"^([{CODE_ALPHABET}]{{4}})-?([{CODE_ALPHABET}]{{4}})$")
# кириллица, похожая на латиницу кода: ребёнок набирает код в русской раскладке
_LOOKALIKES = str.maketrans("АВЕКМНРСТХ", "ABEKMHPCTX")


@dataclass(frozen=True)
class NewInvite:
    kind: InviteKind
    token: str  # в ссылку; в базу — token_hash
    code: str  # показывается как XXXX-XXXX; в базу — code_hash

    @property
    def token_hash(self) -> str:
        return digest(self.token)

    @property
    def code_hash(self) -> str:
        return digest(self.code)

    @property
    def display_code(self) -> str:
        return f"{self.code[:4]}-{self.code[4:]}"

    def link(self, bot_username: str) -> str:
        return f"https://max.ru/{bot_username}?start={start_payload(self.kind, self.token)}"


def new_invite(kind: InviteKind) -> NewInvite:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return NewInvite(kind=kind, token=secrets.token_urlsafe(16), code=code)


def start_payload(kind: InviteKind, token: str) -> str:
    return f"{_PREFIX[kind]}_{token}"


def parse_start_payload(payload: str | None) -> tuple[InviteKind, str] | None:
    """Метка из `bot_started`; неизвестная или битая — None (обычный старт)."""
    prefix, separator, token = (payload or "").partition("_")
    kind = _KIND_BY_PREFIX.get(prefix)
    if not separator or kind is None or not _TOKEN.match(token):
        return None
    return kind, token


def parse_code(text: str) -> str | None:
    """Запасной код из сообщения: «4F7K-92QD», « 4f7k92qd » → «4F7K92QD»; не код — None."""
    match = _CODE.match(text.strip().upper().translate(_LOOKALIKES))
    return match.group(1) + match.group(2) if match else None


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
