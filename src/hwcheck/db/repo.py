"""Профили детей, согласия и приглашения (спецификация онбординга §4, §6, §7).

`ProfileRepository` — всё, что онбордингу нужно от хранилища. `PgProfileRepository` — PostgreSQL;
`InMemoryProfileRepository` (db/memory.py) — для сценарных тестов. Обе реализации проходят одни
контрактные тесты (tests/test_profile_repo.py). Всё, что решает исход гонки (погашение приглашения,
один родитель у ребёнка), делается в одной транзакции с блокировкой строк.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

import asyncpg

from hwcheck.bot.invites import InviteKind, NewInvite

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy

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
        """Родитель отказал: ссылка погашена, родитель не сохраняется. Ребёнка уже подключил
        другой родитель, пока эта ссылка ждала ответа, — `has_parent`: ссылка тоже погашается
        (второй раз не открыть), но уведомления нет — ребёнку уже сообщили о согласии."""
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


_PROFILE = (
    "SELECT p.id, p.user_id, p.parent_user_id, p.grade, p.grade_year, p.subject, "
    "EXISTS (SELECT 1 FROM consents c WHERE c.student_profile_id = p.id "
    "AND c.revoked_at IS NULL) AS has_consent FROM student_profiles p"
)
# класс ссылки ученика — из его профиля, ссылки родителя — из самой ссылки
_INVITE = (
    "SELECT i.token_hash, i.kind, i.created_by, COALESCE(i.grade, p.grade) AS grade, "
    "i.expires_at, i.used_at IS NOT NULL AS used FROM invites i "
    "LEFT JOIN student_profiles p ON p.user_id = i.created_by"
)
_CONSENT = (
    "INSERT INTO consents (parent_hash, student_profile_id, student_hash, policy_version, "
    "given_at) VALUES ($1, $2, $3, $4, $5)"
)

# type-выражение ленивое: PoolConnectionProxy не параметризуется во время выполнения
type Connection = PoolConnectionProxy[asyncpg.Record]


def _account(row: asyncpg.Record) -> Account:
    return Account(
        id=row["id"],
        role=row["role"],
        user_hash=row["max_user_hash"],
        user_id_enc=bytes(row["max_user_id_enc"]),
    )


def _profile(row: asyncpg.Record) -> StudentProfile:
    return StudentProfile(
        id=row["id"],
        user_id=row["user_id"],
        parent_user_id=row["parent_user_id"],
        grade=row["grade"],
        grade_year=row["grade_year"],
        subject=row["subject"],
        has_consent=row["has_consent"],
    )


def _invite(row: asyncpg.Record) -> Invite:
    return Invite(
        token_hash=row["token_hash"],
        kind=row["kind"],
        created_by=row["created_by"],
        grade=row["grade"],
        expires_at=row["expires_at"],
        used=row["used"],
    )


async def _profile_by_id(conn: Connection, profile_id: int) -> StudentProfile:
    row = await conn.fetchrow(f"{_PROFILE} WHERE p.id = $1", profile_id)
    assert row is not None
    return _profile(row)


async def _get_or_create(
    conn: Connection, user_hash: str, user_id_enc: bytes, role: Role
) -> Account:
    await conn.execute(
        "INSERT INTO users (max_user_hash, max_user_id_enc, role) VALUES ($1, $2, $3) "
        "ON CONFLICT (max_user_hash) DO NOTHING",
        user_hash,
        user_id_enc,
        role,
    )
    row = await conn.fetchrow("SELECT * FROM users WHERE max_user_hash = $1", user_hash)
    assert row is not None
    return _account(row)


async def _lock_usable(
    conn: Connection, token_hash: str, kind: InviteKind, now: datetime
) -> tuple[asyncpg.Record | None, InviteResult]:
    """Приглашение под блокировкой до конца транзакции: второй родитель ждёт итога первого."""
    row = await conn.fetchrow("SELECT * FROM invites WHERE token_hash = $1 FOR UPDATE", token_hash)
    if row is None or row["kind"] != kind:
        return None, "invalid"
    if row["used_at"] is not None:
        return None, "used"
    if row["expires_at"] <= now:
        return None, "expired"
    return row, "ok"


class PgProfileRepository:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def get_account(self, user_hash: str) -> Account | None:
        row = await self._pool.fetchrow("SELECT * FROM users WHERE max_user_hash = $1", user_hash)
        return _account(row) if row is not None else None

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        async with self._pool.acquire() as conn:
            return await _get_or_create(conn, user_hash, user_id_enc, role)

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        async with self._pool.acquire() as conn, conn.transaction():
            user_id = await conn.fetchval(
                "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
                "VALUES ($1, $2, 'student') ON CONFLICT (max_user_hash) DO NOTHING RETURNING id",
                user_hash,
                user_id_enc,
            )
            if user_id is None:
                return None
            profile_id = await conn.fetchval(
                "INSERT INTO student_profiles (user_id, grade, grade_year, grade_asked_year) "
                "VALUES ($1, $2, $3, $3) RETURNING id",
                user_id,
                grade,
                year,
            )
            return await _profile_by_id(conn, profile_id)

    async def own_profile(self, user_id: int) -> StudentProfile | None:
        row = await self._pool.fetchrow(f"{_PROFILE} WHERE p.user_id = $1", user_id)
        return _profile(row) if row is not None else None

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        rows = await self._pool.fetch(
            f"{_PROFILE} WHERE p.parent_user_id = $1 ORDER BY p.id", parent_user_id
        )
        return [_profile(row) for row in rows]

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM student_profiles p WHERE p.parent_user_id = $1 AND p.user_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM consents c WHERE c.student_profile_id = p.id "
                "AND c.revoked_at IS NULL)",
                parent_user_id,
            )
            profile_id = await conn.fetchval(
                "INSERT INTO student_profiles (parent_user_id, grade, grade_year, "
                "grade_asked_year) VALUES ($1, $2, $3, $3) RETURNING id",
                parent_user_id,
                grade,
                year,
            )
            return await _profile_by_id(conn, profile_id)

    async def set_subject(self, profile_id: int, subject: str) -> None:
        await self._pool.execute(
            "UPDATE student_profiles SET subject = $2 WHERE id = $1", profile_id, subject
        )

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None:
        await self._pool.execute(
            "INSERT INTO subject_waitlist (user_hash, subject, grade) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            user_hash,
            subject,
            grade,
        )

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        status = await self._pool.execute(
            "INSERT INTO consents (parent_hash, student_profile_id, policy_version, given_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (student_profile_id) WHERE revoked_at IS NULL DO NOTHING",
            parent_hash,
            profile_id,
            policy_version,
            now,
        )
        return bool(status == "INSERT 0 1")

    async def create_invite(
        self,
        invite: NewInvite,
        created_by: int,
        expires_at: datetime,
        *,
        grade: int | None = None,
        policy_version: str | None = None,
        consent_at: datetime | None = None,
    ) -> None:
        await self._pool.execute(
            "INSERT INTO invites (token_hash, code_hash, kind, created_by, grade, "
            "consent_given_at, policy_version, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            invite.token_hash,
            invite.code_hash,
            invite.kind,
            created_by,
            grade,
            consent_at,
            policy_version,
            expires_at,
        )

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None:
        if token_hash is not None:
            row = await self._pool.fetchrow(f"{_INVITE} WHERE i.token_hash = $1", token_hash)
        elif code_hash is not None:
            row = await self._pool.fetchrow(f"{_INVITE} WHERE i.code_hash = $1", code_hash)
        else:
            return None
        return _invite(row) if row is not None else None

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        rows = await self._pool.fetch(
            f"{_INVITE} WHERE i.created_by = $1 AND i.kind = 'parent_invites_student' "
            "AND i.used_at IS NULL AND i.expires_at > $2 ORDER BY i.expires_at",
            parent_user_id,
            now,
        )
        return [_invite(row) for row in rows]

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "student_invites_parent", now)
            if invite is None:
                return LinkOutcome(result)
            role = await conn.fetchval(
                "SELECT role FROM users WHERE max_user_hash = $1", parent_hash
            )
            if role is not None and role != "parent":
                return LinkOutcome("role_mismatch")
            profile = await conn.fetchrow(
                "SELECT id, parent_user_id FROM student_profiles WHERE user_id = $1 FOR UPDATE",
                invite["created_by"],
            )
            if profile is None:
                return LinkOutcome("invalid")
            if profile["parent_user_id"] is not None:
                return LinkOutcome("has_parent")
            parent = await _get_or_create(conn, parent_hash, parent_id_enc, "parent")
            if parent.role != "parent":
                # аккаунт ученика успел появиться после проверки роли выше — записей ещё не было
                return LinkOutcome("role_mismatch")
            child = await conn.fetchrow(
                "SELECT max_user_hash, max_user_id_enc FROM users WHERE id = $1",
                invite["created_by"],
            )
            assert child is not None
            await conn.execute(
                "UPDATE student_profiles SET parent_user_id = $1 WHERE id = $2",
                parent.id,
                profile["id"],
            )
            await conn.execute(
                _CONSENT, parent_hash, profile["id"], child["max_user_hash"], policy_version, now
            )
            await conn.execute(
                "UPDATE invites SET used_at = $1, used_by = $2 WHERE token_hash = $3",
                now,
                parent.id,
                token_hash,
            )
            linked = await _profile_by_id(conn, profile["id"])
            return LinkOutcome("ok", linked, bytes(child["max_user_id_enc"]))

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "student_invites_parent", now)
            if invite is None:
                return LinkOutcome(result)
            profile = await conn.fetchrow(
                "SELECT parent_user_id FROM student_profiles WHERE user_id = $1 FOR UPDATE",
                invite["created_by"],
            )
            await conn.execute(
                "UPDATE invites SET used_at = $1 WHERE token_hash = $2", now, token_hash
            )
            if profile is not None and profile["parent_user_id"] is not None:
                # другой родитель успел согласиться, пока эта ссылка ждала ответа (§11)
                return LinkOutcome("has_parent")
            child_enc = await conn.fetchval(
                "SELECT max_user_id_enc FROM users WHERE id = $1", invite["created_by"]
            )
            return LinkOutcome("ok", notify=bytes(child_enc))

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "parent_invites_student", now)
            if invite is None:
                return LinkOutcome(result)
            existing = await conn.fetchrow(
                "SELECT id, role FROM users WHERE max_user_hash = $1", child_hash
            )
            if existing is not None and existing["role"] != "student":
                return LinkOutcome("role_mismatch")
            if existing is None:
                child_id = await conn.fetchval(
                    "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
                    "VALUES ($1, $2, 'student') RETURNING id",
                    child_hash,
                    child_id_enc,
                )
                profile_id = await conn.fetchval(
                    "INSERT INTO student_profiles (user_id, parent_user_id, grade, grade_year, "
                    "grade_asked_year) VALUES ($1, $2, $3, $4, $4) RETURNING id",
                    child_id,
                    invite["created_by"],
                    invite["grade"],
                    year,
                )
            else:
                child_id = existing["id"]
                own = await conn.fetchrow(
                    "SELECT id, parent_user_id FROM student_profiles WHERE user_id = $1 FOR UPDATE",
                    child_id,
                )
                if own is None:
                    return LinkOutcome("invalid")
                if own["parent_user_id"] is not None:
                    return LinkOutcome("has_parent")
                profile_id = own["id"]
                await conn.execute(
                    "UPDATE student_profiles SET parent_user_id = $1 WHERE id = $2",
                    invite["created_by"],
                    profile_id,
                )
            parent = await conn.fetchrow(
                "SELECT max_user_hash, max_user_id_enc FROM users WHERE id = $1",
                invite["created_by"],
            )
            assert parent is not None
            await conn.execute(
                _CONSENT,
                parent["max_user_hash"],
                profile_id,
                child_hash,
                invite["policy_version"],
                invite["consent_given_at"],
            )
            await conn.execute(
                "UPDATE invites SET used_at = $1, used_by = $2 WHERE token_hash = $3",
                now,
                child_id,
                token_hash,
            )
            linked = await _profile_by_id(conn, profile_id)
            return LinkOutcome("ok", linked, bytes(parent["max_user_id_enc"]))

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        # апдейты обрабатываются последовательно (runner.py) — гонки попыток одного пользователя нет
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM login_attempts WHERE user_hash = $1 AND attempted_at <= $2",
                user_hash,
                now - window,
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM login_attempts WHERE user_hash = $1", user_hash
            )
            if count >= limit:
                return False
            await conn.execute(
                "INSERT INTO login_attempts (user_hash, attempted_at) VALUES ($1, $2)",
                user_hash,
                now,
            )
            return True
