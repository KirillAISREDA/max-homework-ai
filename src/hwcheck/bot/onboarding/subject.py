"""Шаг «предмет» (спецификация §4.1 п.3, §4.7 п.1, §5) — для ученика и для ребёнка 1–4 класса."""

from __future__ import annotations

from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.subjects import subject_by_code
from hwcheck.db.repo import StudentProfile


class SubjectStep:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def ask(self, actor: Actor, profile: StudentProfile) -> None:
        question = texts.SUBJECT_PARENT if profile.sent_by_parent else texts.SUBJECT_STUDENT
        await self._ctx.reply(actor, question, texts.subject_keyboard(profile.grade))

    async def choose(self, actor: Actor, profile: StudentProfile, code: str) -> bool:
        """True — предмет записан. Недоступный — лист ожидания и выбор заново; предмет не своего
        класса или выдуманный payload — просто выбор заново."""
        ctx = self._ctx
        subject = subject_by_code(code)
        if subject is None or profile.grade not in subject.grades:
            await self.ask(actor, profile)
            return False
        ctx.log(
            "onboarding_subject_chosen",
            actor,
            subject=code,
            available=subject.available,
            sender="parent" if profile.sent_by_parent else "student",
        )
        if not subject.available:
            await ctx.repo.add_to_waitlist(actor.user_hash, code, profile.grade)
            ctx.log("subject_waitlist", actor, subject=code, grade=profile.grade)
            await ctx.reply(actor, texts.SUBJECT_SOON.format(title=subject.title))
            await self.ask(actor, profile)
            return False
        await ctx.repo.set_subject(profile.id, code)
        return True
