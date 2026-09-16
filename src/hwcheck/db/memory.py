"""Хранилище профилей в памяти процесса — для сценарных тестов онбординга.

Повторяет поведение PgProfileRepository, включая исходы гонок; расхождение ловят контрактные тесты
tests/test_profile_repo.py, которые гоняют обе реализации.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hwcheck.bot.invites import InviteKind, NewInvite
from hwcheck.db.repo import Account, Invite, InviteResult, LinkOutcome, Role, StudentProfile


@dataclass
class _Profile:
    id: int
    user_id: int | None
    parent_user_id: int | None
    grade: int
    grade_year: int
    subject: str | None = None


@dataclass
class _Invite:
    token_hash: str
    code_hash: str
    kind: InviteKind
    created_by: int
    expires_at: datetime
    grade: int | None
    policy_version: str | None
    consent_at: datetime | None
    used_at: datetime | None = None


@dataclass
class _Consent:
    parent_hash: str
    profile_id: int
    student_hash: str | None
    policy_version: str
    given_at: datetime
    revoked_at: datetime | None = None


class InMemoryProfileRepository:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._profiles: dict[int, _Profile] = {}
        self._invites: dict[str, _Invite] = {}
        self._attempts: dict[str, list[datetime]] = {}
        self._last_id = 0
        self.consents: list[_Consent] = []
        self.waitlist: set[tuple[str, str]] = set()

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _account_by_id(self, account_id: int) -> Account:
        return next(a for a in self._accounts.values() if a.id == account_id)

    def _own(self, user_id: int) -> _Profile | None:
        return next((p for p in self._profiles.values() if p.user_id == user_id), None)

    def _has_consent(self, profile_id: int) -> bool:
        return any(c.profile_id == profile_id and c.revoked_at is None for c in self.consents)

    def _view(self, profile: _Profile) -> StudentProfile:
        return StudentProfile(
            id=profile.id,
            user_id=profile.user_id,
            parent_user_id=profile.parent_user_id,
            grade=profile.grade,
            grade_year=profile.grade_year,
            subject=profile.subject,
            has_consent=self._has_consent(profile.id),
        )

    async def get_account(self, user_hash: str) -> Account | None:
        return self._accounts.get(user_hash)

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        if user_hash not in self._accounts:
            self._accounts[user_hash] = Account(self._next_id(), role, user_hash, user_id_enc)
        return self._accounts[user_hash]

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        if user_hash in self._accounts:
            return None
        account = await self.get_or_create_account(user_hash, user_id_enc, "student")
        profile = _Profile(self._next_id(), account.id, None, grade, year)
        self._profiles[profile.id] = profile
        return self._view(profile)

    async def own_profile(self, user_id: int) -> StudentProfile | None:
        profile = self._own(user_id)
        return self._view(profile) if profile is not None else None

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        ordered = sorted(self._profiles.values(), key=lambda p: p.id)
        return [self._view(p) for p in ordered if p.parent_user_id == parent_user_id]

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        for profile in list(self._profiles.values()):
            unfinished = profile.user_id is None and not self._has_consent(profile.id)
            if profile.parent_user_id == parent_user_id and unfinished:
                del self._profiles[profile.id]
        profile = _Profile(self._next_id(), None, parent_user_id, grade, year)
        self._profiles[profile.id] = profile
        return self._view(profile)

    async def set_subject(self, profile_id: int, subject: str) -> None:
        if profile_id in self._profiles:
            self._profiles[profile_id].subject = subject

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None:
        self.waitlist.add((user_hash, subject))

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        if self._has_consent(profile_id):
            return False
        self.consents.append(_Consent(parent_hash, profile_id, None, policy_version, now))
        return True

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
        self._invites[invite.token_hash] = _Invite(
            invite.token_hash,
            invite.code_hash,
            invite.kind,
            created_by,
            expires_at,
            grade,
            policy_version,
            consent_at,
        )

    def _invite_view(self, invite: _Invite) -> Invite:
        grade = invite.grade
        if grade is None:
            own = self._own(invite.created_by)
            grade = own.grade if own is not None else None
        used = invite.used_at is not None
        return Invite(
            invite.token_hash, invite.kind, invite.created_by, grade, invite.expires_at, used
        )

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None:
        for invite in self._invites.values():
            if (token_hash is not None and invite.token_hash == token_hash) or (
                code_hash is not None and invite.code_hash == code_hash
            ):
                return self._invite_view(invite)
        return None

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        return [
            self._invite_view(i)
            for i in sorted(self._invites.values(), key=lambda i: i.expires_at)
            if i.created_by == parent_user_id
            and i.kind == "parent_invites_student"
            and i.used_at is None
            and i.expires_at > now
        ]

    def _usable(
        self, token_hash: str, kind: InviteKind, now: datetime
    ) -> tuple[_Invite | None, InviteResult]:
        invite = self._invites.get(token_hash)
        if invite is None or invite.kind != kind:
            return None, "invalid"
        if invite.used_at is not None:
            return None, "used"
        if invite.expires_at <= now:
            return None, "expired"
        return invite, "ok"

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        invite, result = self._usable(token_hash, "student_invites_parent", now)
        if invite is None:
            return LinkOutcome(result)
        existing = self._accounts.get(parent_hash)
        if existing is not None and existing.role != "parent":
            return LinkOutcome("role_mismatch")
        profile = self._own(invite.created_by)
        if profile is None:
            return LinkOutcome("invalid")
        if profile.parent_user_id is not None:
            return LinkOutcome("has_parent")
        parent = await self.get_or_create_account(parent_hash, parent_id_enc, "parent")
        child = self._account_by_id(invite.created_by)
        profile.parent_user_id = parent.id
        consent = _Consent(parent_hash, profile.id, child.user_hash, policy_version, now)
        self.consents.append(consent)
        invite.used_at = now
        return LinkOutcome("ok", self._view(profile), child.user_id_enc)

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        invite, result = self._usable(token_hash, "student_invites_parent", now)
        if invite is None:
            return LinkOutcome(result)
        invite.used_at = now
        return LinkOutcome("ok", notify=self._account_by_id(invite.created_by).user_id_enc)

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        invite, result = self._usable(token_hash, "parent_invites_student", now)
        if invite is None:
            return LinkOutcome(result)
        if invite.grade is None or invite.policy_version is None or invite.consent_at is None:
            return LinkOutcome("invalid")
        existing = self._accounts.get(child_hash)
        if existing is not None and existing.role != "student":
            return LinkOutcome("role_mismatch")
        parent = self._account_by_id(invite.created_by)
        if existing is None:
            account = await self.get_or_create_account(child_hash, child_id_enc, "student")
            profile = _Profile(self._next_id(), account.id, parent.id, invite.grade, year)
            self._profiles[profile.id] = profile
        else:
            own = self._own(existing.id)
            if own is None:
                return LinkOutcome("invalid")
            if own.parent_user_id is not None:
                return LinkOutcome("has_parent")
            own.parent_user_id = parent.id
            profile = own
        consent = _Consent(
            parent.user_hash, profile.id, child_hash, invite.policy_version, invite.consent_at
        )
        self.consents.append(consent)
        invite.used_at = now
        return LinkOutcome("ok", self._view(profile), parent.user_id_enc)

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        recent = [t for t in self._attempts.get(user_hash, []) if t > now - window]
        if len(recent) >= limit:
            self._attempts[user_hash] = recent
            return False
        self._attempts[user_hash] = [*recent, now]
        return True
