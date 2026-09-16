"""Ученик в своём MAX (спецификация §4.1, §10.2): класс, ссылка родителю, ожидание согласия.

1–4 класс дальше выбора класса не идёт: ссылка на бота для родителя, в базе ничего (§4.7).
"""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import INVITE_TTL_DAYS, new_invite
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import StudentProfile


class StudentSteps:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def choose_grade(self, actor: Actor, grade: int) -> None:
        ctx = self._ctx
        if grade <= PARENT_SENDS_UP_TO_GRADE:
            ctx.log("onboarding_parent_required", actor, grade=grade)
            await ctx.reply(actor, texts.YOUNG_STUDENT)
            link = texts.bot_link(ctx.bot_username)
            await ctx.reply(actor, texts.BOT_LINK_FOR_PARENT.format(link=link))
            return
        profile = await ctx.repo.create_student(
            actor.user_hash, ctx.encrypted_id(actor), grade, ctx.school_year()
        )
        if profile is None:  # аккаунт уже есть: старая кнопка, маршрутизатор покажет текущий шаг
            return
        ctx.log("onboarding_grade_chosen", actor, grade=grade, role="student")
        await self._subjects.ask(actor, profile)

    async def after_subject(self, actor: Actor, profile: StudentProfile) -> None:
        """Предмет выбран: родитель уже подключён (ребёнок пришёл по его ссылке) — инструкция."""
        if profile.has_consent:
            await self._ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        else:
            await self.invite_parent(actor, profile)

    async def invite_parent(self, actor: Actor, profile: StudentProfile) -> None:
        """Новая ссылка родителю: в базе только HMAC токена и кода, сами они — в сообщении."""
        ctx = self._ctx
        assert profile.user_id is not None  # ссылку родителю создаёт ученик со своим MAX
        invite = new_invite("student_invites_parent")
        expires_at = ctx.now() + timedelta(days=INVITE_TTL_DAYS)
        await ctx.repo.create_invite(invite, profile.user_id, expires_at)
        ctx.log("invite_created", actor, kind=invite.kind)
        await ctx.reply(actor, texts.NEED_PARENT, texts.example_keyboard())
        message = texts.PARENT_INVITE_MESSAGE.format(
            link=invite.link(ctx.bot_username), code=invite.display_code
        )
        await ctx.reply(actor, message)

    async def remind_waiting(self, actor: Actor) -> None:
        await self._ctx.reply(actor, texts.WAITING_PARENT, texts.waiting_parent_keyboard())

    async def block_photo(self, actor: Actor) -> None:
        """До согласия фото не скачивается, не сохраняется и не уходит в GigaChat (§10.2)."""
        await self._ctx.reply(actor, texts.PHOTO_BLOCKED, texts.waiting_parent_keyboard())
