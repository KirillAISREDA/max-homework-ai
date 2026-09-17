"""Родитель (спецификация §4.3, §4.7): класс ребёнка; дети 1–4 класса, за которых фото присылает
родитель; ссылка ребёнку 5–9 класса; список детей; «Чья домашка?» при нескольких детях 1–4."""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import INVITE_TTL_DAYS, new_invite
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.pages import TEXTBOOK_TTL_S
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import Account, StudentProfile

# фото одной домашки приходят рядом (учебник, затем тетрадь): выбор ребёнка и ожидающие фото живут
# столько же, сколько условия учебника
OWNER_TTL_S = TEXTBOOK_TTL_S


class ParentSteps:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def _parent(self, actor: Actor, account: Account | None) -> Account:
        if account is not None:
            return account
        return await self._ctx.repo.get_or_create_account(
            actor.user_hash, self._ctx.encrypted_id(actor), "parent"
        )

    async def ask_grade(self, actor: Actor) -> None:
        await self._ctx.reply(actor, texts.PARENT_GRADE, texts.grade_keyboard("pgrade"))

    async def choose_grade(self, actor: Actor, account: Account | None, grade: int) -> None:
        """1–4 класс — ребёнок без своего MAX сразу в базе; 5–9 — согласие, класс едет в payload."""
        ctx = self._ctx
        ctx.log("onboarding_grade_chosen", actor, grade=grade, role="parent")
        if grade <= PARENT_SENDS_UP_TO_GRADE:
            parent = await self._parent(actor, account)
            child = await ctx.repo.start_child_by_parent(parent.id, grade, ctx.school_year())
            await self._subjects.ask(actor, child)
            return
        intro = texts.PARENT_FIRST_CONSENT.format(grade=grade)
        buttons = texts.consent_keyboard(f"ob:pconsent:{grade}")
        await ctx.reply(actor, texts.consent_text(intro), buttons)

    async def ask_consent(self, actor: Actor) -> None:
        buttons = texts.consent_keyboard("ob:consent")
        await self._ctx.reply(actor, texts.consent_text(texts.PARENT_SENDS_CONSENT), buttons)

    async def give_consent(self, actor: Actor, child: StudentProfile) -> None:
        ctx = self._ctx
        if await ctx.repo.give_parent_consent(child.id, actor.user_hash, POLICY_VERSION, ctx.now()):
            ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="parent_sends")
        await ctx.reply(actor, texts.INSTRUCTION_PARENT, texts.add_child_keyboard())

    async def invite_child(self, actor: Actor, account: Account | None, grade: int) -> None:
        """«Согласен» за ребёнка 5–9 класса: аккаунт родителя и ссылка с классом и согласием."""
        ctx = self._ctx
        parent = await self._parent(actor, account)
        invite = new_invite("parent_invites_student")
        now = ctx.now()
        await ctx.repo.create_invite(
            invite,
            parent.id,
            now + timedelta(days=INVITE_TTL_DAYS),
            grade=grade,
            policy_version=POLICY_VERSION,
            consent_at=now,
        )
        ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="parent_first")
        ctx.log("invite_created", actor, kind=invite.kind)
        await ctx.reply(actor, texts.FORWARD_TO_CHILD)
        message = texts.CHILD_INVITE_MESSAGE.format(
            link=invite.link(ctx.bot_username), code=invite.display_code
        )
        await ctx.reply(actor, message)

    async def show_status(self, actor: Actor, account: Account) -> None:
        """До меню (этап 3): список детей и [Добавить ребёнка]; никого нет — класс ребёнка."""
        ctx = self._ctx
        children = [c for c in await ctx.repo.children(account.id) if c.has_consent]
        waiting = await ctx.repo.open_child_invites(account.id, ctx.now())
        if not children and not waiting:
            await self.ask_grade(actor)
            return
        status = texts.parent_status(children, [invite.grade for invite in waiting])
        await ctx.reply(actor, status, texts.add_child_keyboard())

    async def young_children(self, account: Account) -> list[StudentProfile]:
        children = await self._ctx.repo.children(account.id)
        return [c for c in children if c.sent_by_parent and c.has_consent]

    async def on_photo(self, actor: Actor, account: Account, urls: list[str]) -> list[str] | None:
        """Фото для проверки или None. Несколько детей 1–4 — «Чья домашка?», фото ждут ответа."""
        ctx = self._ctx
        young = await self.young_children(account)
        if not young:
            await ctx.reply(actor, texts.PARENT_PHOTO_NO_YOUNG, texts.add_child_keyboard())
            return None
        state = await ctx.states.get(actor.user_hash)
        now = ctx.now().timestamp()
        chosen = state.child_id in {c.id for c in young} and _fresh(state.child_chosen_at, now)
        if len(young) == 1 or chosen:
            return urls
        waiting = bool(state.pending_photos) and _fresh(state.pending_at, now)
        pending = [*state.pending_photos, *urls] if waiting else list(urls)
        pending_at = state.pending_at if waiting else now
        update = {"pending_photos": pending, "pending_at": pending_at}
        await ctx.states.set(actor.user_hash, state.model_copy(update=update))
        if not waiting:
            ctx.log("homework_owner_asked", actor, n_children=len(young))
            await ctx.reply(actor, texts.WHOSE_HOMEWORK, texts.whose_keyboard(young))
        return None

    async def homework_subject(self, actor: Actor, account: Account) -> str:
        """Предмет ребёнка, чью домашку проверяем: выбранного кнопкой «Чья домашка?» или
        единственного ребёнка 1–4 класса. Иначе математика — предмет по умолчанию."""
        young = await self.young_children(account)
        state = await self._ctx.states.get(actor.user_hash)
        chosen = next((c for c in young if c.id == state.child_id), None)
        child = chosen or (young[0] if len(young) == 1 else None)
        return (child.subject if child is not None else None) or "math"

    async def choose_owner(self, actor: Actor, account: Account, child_id: int) -> list[str] | None:
        ctx = self._ctx
        if child_id not in {c.id for c in await self.young_children(account)}:
            return None
        state = await ctx.states.get(actor.user_hash)
        now = ctx.now().timestamp()
        if not state.pending_photos or not _fresh(state.pending_at, now):
            await ctx.reply(actor, texts.PHOTOS_EXPIRED)
            return None
        update: dict[str, list[str] | int | float | None] = {
            "pending_photos": [],
            "pending_at": None,
            "child_id": child_id,
            "child_chosen_at": now,
        }
        await ctx.states.set(actor.user_hash, state.model_copy(update=update))
        ctx.log("homework_owner_chosen", actor)
        return state.pending_photos


def _fresh(saved_at: float | None, now: float) -> bool:
    return saved_at is not None and now - saved_at < OWNER_TTL_S
