"""Метки ссылки на бота: приглашения «ребёнок ↔ родитель» (спецификация онбординга §4.4, §7)
и источник трафика.

Ссылка `https://max.ru/<бот>?start=p_<токен>` (ребёнок зовёт родителя) или `c_<токен>` (родитель
зовёт ребёнка); запасной код — 8 знаков без похожих 0/O и 1/I. В базе — только HMAC токена и кода
(ключ ID_HASH_KEY): утечка таблицы или бэкапа не даёт подобрать и погасить чужое приглашение.

Источник трафика — `?start=s_<метка>`: своя метка на каждый канал, пост или чат. Антифрод
конкурса просит сведения об источниках трафика (Положение, Прил. 2 п. 5).
"""

import re
import secrets
from dataclasses import dataclass
from typing import Literal

from hwcheck.events import keyed_digest

InviteKind = Literal["student_invites_parent", "parent_invites_student"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
INVITE_TTL_DAYS = 7
MAX_START_PAYLOAD = 128  # лимит MAX на метку ?start=

_PREFIX: dict[InviteKind, str] = {"student_invites_parent": "p", "parent_invites_student": "c"}
_KIND_BY_PREFIX: dict[str, InviteKind] = {prefix: kind for kind, prefix in _PREFIX.items()}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CODE = re.compile(rf"^([{CODE_ALPHABET}]{{4}})-?([{CODE_ALPHABET}]{{4}})$")
_SOURCE_PREFIX = "s"
# короткая латинская метка: в журнал идёт только она, а не произвольный текст из ссылки
_SOURCE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# служебные значения поля source в журнале: метка канала с таким именем слилась бы с ними
RESERVED_SOURCES = frozenset({"invite", "direct", "unknown"})
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


def parse_source(payload: str | None) -> str | None:
    """Метка источника из `bot_started`: «s_kanal-1» → «kanal-1»; не метка — None."""
    prefix, separator, tag = (payload or "").partition("_")
    tag = tag.lower()
    if prefix != _SOURCE_PREFIX or not separator or not _is_source(tag):
        return None
    return tag


def _is_source(tag: str) -> bool:
    """Целиком по шаблону (`fullmatch`: `$` пропустил бы перевод строки в конце) и не служебное
    слово журнала."""
    return _SOURCE.fullmatch(tag) is not None and tag not in RESERVED_SOURCES


def source_link(bot_username: str, tag: str) -> str:
    """Ссылка на бота для канала или поста; негодная метка — ошибка, а не ссылка без учёта."""
    if not _is_source(tag):
        reserved = ", ".join(sorted(RESERVED_SOURCES))
        raise ValueError(
            f"метка источника — латиница, цифры и дефис, до 32 знаков, кроме {reserved}: {tag!r}"
        )
    return f"https://max.ru/{bot_username}?start={_SOURCE_PREFIX}_{tag}"


def parse_code(text: str) -> str | None:
    """Запасной код из сообщения: «4F7K-92QD», « 4f7k92qd » → «4F7K92QD»; не код — None."""
    match = _CODE.match(text.strip().upper().translate(_LOOKALIKES))
    return match.group(1) + match.group(2) if match else None


def digest(secret: str) -> str:
    return keyed_digest(secret)
