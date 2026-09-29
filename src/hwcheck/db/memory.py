"""Хранилище профилей в памяти процесса — для сценарных тестов онбординга.

Повторяет поведение PgProfileRepository, включая исходы гонок; расхождение ловят контрактные тесты
tests/test_profile_repo.py, которые гоняют обе реализации. Оно же выполняет контракт хранилища
отчёта родителю (db/reports.py, tests/test_report_repo.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from hwcheck.bot.invites import InviteKind, NewInvite
from hwcheck.db.repo import (
    DEFAULT_NOTIFY_MODE,
    Account,
    Homework,
    HomeworkCounts,
    Invite,
    InviteResult,
    LinkOutcome,
    NotifyMode,
    Role,
    StudentProfile,
)
from hwcheck.db.reports import SubjectTotals, WeeklyStatus


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        # время проверки домашки в базе ставит `now()`; здесь — часы, которые тест двигает сам
        self._clock = clock
        self._accounts: dict[str, Account] = {}
        self._profiles: dict[int, _Profile] = {}
        self._invites: dict[str, _Invite] = {}
        self._attempts: dict[str, list[datetime]] = {}
        self._last_id = 0
        self.consents: list[_Consent] = []
        self.waitlist: set[tuple[str, str]] = set()
        self.homeworks: dict[int, Homework] = {}
        self.notify_modes: dict[int, NotifyMode] = {}
        self.weekly_off: set[int] = set()  # родители, выключившие отчёт раз в неделю
        # отметки недели: (родитель, слот) → `sending` или чем кончилась отправка
        self.weekly_reports: dict[tuple[int, datetime], str] = {}

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _account_by_id(self, account_id: int) -> Account:
        return next(a for a in self._accounts.values() if a.id == account_id)

    def _own(self, user_id: int) -> _Profile | None:
        return next((p for p in self._profiles.values() if p.user_id == user_id), None)

    def _consent_policy(self, profile_id: int) -> str | None:
        active = (
            c.policy_version
            for c in self.consents
            if c.profile_id == profile_id and c.revoked_at is None
        )
        return next(active, None)

    def _has_consent(self, profile_id: int) -> bool:
        return self._consent_policy(profile_id) is not None

    def _view(self, profile: _Profile) -> StudentProfile:
        return StudentProfile(
            id=profile.id,
            user_id=profile.user_id,
            parent_user_id=profile.parent_user_id,
            grade=profile.grade,
            grade_year=profile.grade_year,
            subject=profile.subject,
            has_consent=self._has_consent(profile.id),
            consent_policy=self._consent_policy(profile.id),
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

    async def get_profile(self, profile_id: int) -> StudentProfile | None:
        profile = self._profiles.get(profile_id)
        return self._view(profile) if profile is not None else None

    async def account_by_id(self, account_id: int) -> Account | None:
        return next((a for a in self._accounts.values() if a.id == account_id), None)

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

    async def renew_consent(
        self,
        profile_id: int,
        parent_user_id: int,
        parent_hash: str,
        policy_version: str,
        now: datetime,
    ) -> str | None:
        profile = self._profiles.get(profile_id)
        if profile is None or profile.parent_user_id != parent_user_id:
            return None
        active = next(
            (c for c in self.consents if c.profile_id == profile_id and c.revoked_at is None),
            None,
        )
        if active is None or active.policy_version == policy_version:
            return None
        active.revoked_at = now
        self.consents.append(
            _Consent(parent_hash, profile_id, active.student_hash, policy_version, now)
        )
        return active.policy_version

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
        profile = self._own(invite.created_by)
        invite.used_at = now
        if profile is not None and profile.parent_user_id is not None:
            # другой родитель успел согласиться, пока эта ссылка ждала ответа (§11)
            return LinkOutcome("has_parent")
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

    async def add_homework(
        self, student_id: int, subject: str, counts: HomeworkCounts
    ) -> Homework | None:
        if student_id not in self._profiles:
            return None
        homework = Homework(
            self._next_id(),
            student_id,
            subject,
            counts,
            errors_resolved=0,
            created_at=self._clock(),
        )
        self.homeworks[homework.id] = homework
        return homework

    async def resolve_error(self, homework_id: int) -> Homework | None:
        homework = self.homeworks.get(homework_id)
        if homework is None:
            return None
        resolved = replace(homework, errors_resolved=homework.errors_resolved + 1)
        self.homeworks[homework_id] = resolved
        return resolved

    async def notify_mode(self, parent_user_id: int) -> NotifyMode:
        return self.notify_modes.get(parent_user_id, DEFAULT_NOTIFY_MODE)

    async def set_notify_mode(self, parent_user_id: int, mode: NotifyMode) -> None:
        self.notify_modes[parent_user_id] = mode

    async def totals(
        self, parent_user_id: int, since: datetime, until: datetime
    ) -> list[SubjectTotals]:
        """Контракт `ReportRepository` (db/reports.py): итоги по детям родителя за период."""
        own = {p.id for p in self._profiles.values() if p.parent_user_id == parent_user_id}
        groups: dict[tuple[int, str], list[Homework]] = {}
        for homework in self.homeworks.values():
            checked = homework.created_at
            if homework.student_id in own and checked is not None and since <= checked < until:
                groups.setdefault((homework.student_id, homework.subject), []).append(homework)
        return [_totals(*key, groups[key]) for key in sorted(groups)]

    async def weekly_enabled(self, parent_user_id: int) -> bool:
        return parent_user_id not in self.weekly_off

    async def set_weekly(self, parent_user_id: int, enabled: bool) -> None:
        if enabled:
            self.weekly_off.discard(parent_user_id)
        else:
            self.weekly_off.add(parent_user_id)

    def _consented_before(self, parent_user_id: int, slot: datetime) -> bool:
        own = {p.id for p in self._profiles.values() if p.parent_user_id == parent_user_id}
        return any(
            c.profile_id in own and c.revoked_at is None and c.given_at < slot
            for c in self.consents
        )

    async def due_parents(self, slot: datetime) -> list[Account]:
        return [
            account
            for account in sorted(self._accounts.values(), key=lambda a: a.id)
            if account.role == "parent"
            and account.id not in self.weekly_off
            and self._consented_before(account.id, slot)
            and (account.id, slot) not in self.weekly_reports
        ]

    async def claim_weekly(self, parent_user_id: int, slot: datetime) -> bool:
        if (parent_user_id, slot) in self.weekly_reports:
            return False
        self.weekly_reports[(parent_user_id, slot)] = "sending"
        return True

    async def finish_weekly(
        self, parent_user_id: int, slot: datetime, status: WeeklyStatus
    ) -> None:
        if (parent_user_id, slot) in self.weekly_reports:
            self.weekly_reports[(parent_user_id, slot)] = status


def _totals(student_id: int, subject: str, homeworks: list[Homework]) -> SubjectTotals:
    counts = HomeworkCounts(
        total=sum(h.counts.total for h in homeworks),
        correct=sum(h.counts.correct for h in homeworks),
        wrong=sum(h.counts.wrong for h in homeworks),
        uncertain=sum(h.counts.uncertain for h in homeworks),
    )
    resolved = sum(h.errors_resolved for h in homeworks)
    return SubjectTotals(student_id, subject, len(homeworks), counts, errors_resolved=resolved)
