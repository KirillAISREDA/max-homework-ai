"""Профили детей, согласия и приглашения (спецификация онбординга §4, §6, §7).

`ProfileRepository` — всё, что онбордингу нужно от хранилища. `PgProfileRepository` — PostgreSQL;
`InMemoryProfileRepository` (db/memory.py) — для сценарных тестов. Обе реализации проходят одни
контрактные тесты (tests/test_profile_repo.py). Всё, что решает исход гонки (погашение приглашения,
один родитель у ребёнка), делается в одной транзакции с блокировкой строк.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from hwcheck.bot.invites import InviteKind, NewInvite

Role = Literal["student", "parent"]
# исход открытия приглашения — поле result события invite_opened (§12)
InviteResult = Literal["ok", "expired", "used", "invalid", "role_mismatch", "has_parent"]


@dataclass(frozen=True)
class Account:
    id: int
    role: Role
    user_hash: str
    user_id_enc: bytes  # Fernet(USER_ID_KEY): написать пользователю первым


@dataclass(frozen=True)
class StudentProfile:
    id: int
    user_id: int | None  # свой MAX ребёнка (5–9 класс); None — фото присылает родитель
    parent_user_id: int | None
    grade: int
    grade_year: int
    subject: str | None
    has_consent: bool

    @property
    def sent_by_parent(self) -> bool:
        return self.user_id is None


@dataclass(frozen=True)
class Invite:
    token_hash: str
    kind: InviteKind
    created_by: int
    grade: int | None  # класс ребёнка: из ссылки родителя или из профиля ученика
    expires_at: datetime
    used: bool


@dataclass(frozen=True)
class LinkOutcome:
    """Итог погашения приглашения. `profile` — ребёнок после привязки; `notify` — шифротекст id
    второй стороны, которой бот пишет о результате."""

    result: InviteResult
    profile: StudentProfile | None = None
    notify: bytes | None = None


class ProfileRepository(Protocol):
    async def get_account(self, user_hash: str) -> Account | None: ...

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        """Новый аккаунт с ролью или существующий — со своей ролью (её проверяет вызывающий)."""
        ...

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        """Аккаунт ученика и профиль одной транзакцией; аккаунт уже есть — None."""
        ...

    async def own_profile(self, user_id: int) -> StudentProfile | None: ...

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        """Дети родителя в порядке добавления: и со своим MAX, и 1–4 класса."""
        ...

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        """Ребёнок 1–4 класса; незавершённый (без согласия) заменяется, а не копится."""
        ...

    async def set_subject(self, profile_id: int, subject: str) -> None: ...

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None: ...

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        """Согласие на ребёнка 1–4 класса; активное уже есть — False."""
        ...

    async def create_invite(
        self,
        invite: NewInvite,
        created_by: int,
        expires_at: datetime,
        *,
        grade: int | None = None,
        policy_version: str | None = None,
        consent_at: datetime | None = None,
    ) -> None: ...

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None: ...

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        """Непогашенные живые ссылки родителя ребёнку 5–9 класса."""
        ...

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        """Родитель согласился по ссылке ученика: аккаунт родителя, связка, согласие, погашение."""
        ...

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        """Родитель отказал: ссылка погашена, родитель не сохраняется."""
        ...

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        """Ребёнок открыл ссылку родителя: аккаунт и профиль (класс из ссылки), связка, согласие."""
        ...

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        """Учитывает попытку ввода кода; False — лимит за окно исчерпан, попытка не записана."""
        ...
