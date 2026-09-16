"""Ссылки и запасные коды «ребёнок ↔ родитель» (спецификация §4.2, §4.3 п.6, §4.4, §11).

Открытие проверяет ссылку и роль до показа согласия, но исход решает транзакция хранилища: между
открытием и «Согласен» ссылку мог погасить другой родитель. Ссылка ученика, открытая родителем, ждёт
ответа в Redis; ссылку родителя ребёнок погашает сразу — согласие дано при её создании.
"""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import InviteKind, digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import Account, Invite, InviteResult

CODE_ATTEMPTS = 5
CODE_WINDOW = timedelta(hours=1)


class Linking:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def open_link(
        self, actor: Actor, account: Account | None, kind: InviteKind, token: str
    ) -> None:
        invite = await self._ctx.repo.find_invite(token_hash=digest(token))
        await self._open(actor, account, invite, kind, via="link")

    async def open_code(self, actor: Actor, account: Account | None, code: str) -> None:
        ctx = self._ctx
        formal = account is None or account.role == "parent"
        allowed = await ctx.repo.code_attempt(
            actor.user_hash, ctx.now(), limit=CODE_ATTEMPTS, window=CODE_WINDOW
        )
        if not allowed:
            ctx.log("invite_opened", actor, kind=None, via="code", result="rate_limited")
            await ctx.reply(actor, texts.code_rate_limited(formal))
            return
        invite = await ctx.repo.find_invite(code_hash=digest(code))
        if invite is None:
            ctx.log("invite_opened", actor, kind=None, via="code", result="invalid")
            await ctx.reply(actor, texts.code_invalid(formal))
            return
        await self._open(actor, account, invite, invite.kind, via="code")

    async def _open(
        self,
        actor: Actor,
        account: Account | None,
        invite: Invite | None,
        kind: InviteKind,
        via: str,
    ) -> None:
        ctx = self._ctx
        result = await self._precheck(account, invite, kind)
        if invite is None or result != "ok":
            ctx.log("invite_opened", actor, kind=kind, via=via, result=result)
            await ctx.reply(actor, texts.invite_refusal(kind, result))
            return
        if kind == "parent_invites_student":
            await self._join_child(actor, invite, via)
            return
        ctx.log("invite_opened", actor, kind=kind, via=via, result="ok")
        state = await ctx.states.get(actor.user_hash)
        await ctx.states.set(
            actor.user_hash, state.model_copy(update={"pending_invite": invite.token_hash})
        )
        intro = texts.CHILD_ASKS_CONSENT.format(grade=invite.grade)
        buttons = texts.consent_keyboard("ob:accept", "ob:decline")
        await ctx.reply(actor, texts.consent_text(intro), buttons)

    async def _precheck(
        self, account: Account | None, invite: Invite | None, kind: InviteKind
    ) -> InviteResult:
        """Исход до транзакции — чтобы не показывать согласие по заведомо негодной ссылке."""
        if invite is None or invite.kind != kind:
            return "invalid"
        if invite.used:
            return "used"
        if invite.expires_at <= self._ctx.now():
            return "expired"
        wanted = "parent" if kind == "student_invites_parent" else "student"
        if account is not None and account.role != wanted:
            return "role_mismatch"
        # чей профиль проверять: ссылку ученика создал ребёнок, ссылку родителя открывает ребёнок
        child_account = invite.created_by if kind == "student_invites_parent" else None
        if kind == "parent_invites_student" and account is not None:
            child_account = account.id
        if child_account is not None:
            child = await self._ctx.repo.own_profile(child_account)
            if child is not None and child.parent_user_id is not None:
                return "has_parent"
        return "ok"

    async def _join_child(self, actor: Actor, invite: Invite, via: str) -> None:
        """Ребёнок открыл ссылку родителя: привязка и согласие одной транзакцией, затем предмет."""
        ctx = self._ctx
        outcome = await ctx.repo.accept_child_invite(
            invite.token_hash,
            actor.user_hash,
            ctx.encrypted_id(actor),
            ctx.school_year(),
            ctx.now(),
        )
        ctx.log("invite_opened", actor, kind=invite.kind, via=via, result=outcome.result)
        profile, parent = outcome.profile, outcome.notify
        if outcome.result != "ok" or profile is None or parent is None:
            await ctx.reply(actor, texts.invite_refusal(invite.kind, outcome.result))
            return
        ctx.log("child_linked", actor, grade=profile.grade)
        await ctx.reply(actor, texts.CHILD_LINKED)
        if profile.subject is None:
            await self._subjects.ask(actor, profile)
        else:
            await ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        joined = texts.CHILD_JOINED.format(grade=profile.grade)
        await ctx.notify(actor, parent, joined, kind="child_linked")

    async def accept(self, actor: Actor) -> None:
        ctx = self._ctx
        state = await ctx.states.get(actor.user_hash)
        if state.pending_invite is None:
            await ctx.reply(actor, texts.LINK_GONE)
            return
        outcome = await ctx.repo.accept_parent_invite(
            state.pending_invite,
            actor.user_hash,
            ctx.encrypted_id(actor),
            POLICY_VERSION,
            ctx.now(),
        )
        await ctx.states.set(actor.user_hash, state.model_copy(update={"pending_invite": None}))
        profile, child = outcome.profile, outcome.notify
        if outcome.result != "ok" or profile is None or child is None:
            await ctx.reply(actor, texts.invite_refusal("student_invites_parent", outcome.result))
            return
        ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="child_link")
        ctx.log("child_linked", actor, grade=profile.grade)
        await ctx.reply(actor, texts.CONSENT_THANKS)
        allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
        await ctx.notify(actor, child, allowed, kind="consent_given")

    async def decline(self, actor: Actor) -> None:
        ctx = self._ctx
        state = await ctx.states.get(actor.user_hash)
        if state.pending_invite is None:
            await ctx.reply(actor, texts.LINK_GONE)
            return
        outcome = await ctx.repo.decline_parent_invite(state.pending_invite, ctx.now())
        await ctx.states.set(actor.user_hash, state.model_copy(update={"pending_invite": None}))
        if outcome.result != "ok" or outcome.notify is None:
            await ctx.reply(actor, texts.invite_refusal("student_invites_parent", outcome.result))
            return
        ctx.log("consent_declined", actor)
        await ctx.reply(actor, texts.DECLINED)
        await ctx.notify(
            actor,
            outcome.notify,
            texts.PARENT_NOT_ALLOWED,
            kind="consent_declined",
            buttons=texts.waiting_parent_keyboard(),
        )
