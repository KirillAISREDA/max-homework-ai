"""Обновление согласия: семья, согласившаяся по прежней политике, получает новое — единое.

Политика меняется (v2 — фото страниц читает сторонняя модель), а согласие у семьи остаётся со
своей версией. Родителя просим согласиться заново, но ребёнок не ждёт: пока нового согласия
нет, его фото читает прежняя модель (`bot/handlers.py`, `policy.allows_foreign_models`).

Согласие даёт родитель из своего чата: за ребёнка 1–4 класса — там же, где присылает фото, за
ребёнка 5–9 класса — сообщением ему. Напоминаем не чаще раза в `REMIND_EVERY`.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from hwcheck.bot.notifier import child_label
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.db.repo import Account, StudentProfile

logger = logging.getLogger(__name__)

REMIND_EVERY = timedelta(days=3)
NOTIFY_KIND = "consent_renewal"


class ConsentRenewal:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def remind(self, actor: Actor, child: StudentProfile | None) -> None:
        """Перед проверкой: согласие по прежней политике — попросить родителя о новом.

        Сбой напоминания проверку не останавливает: ребёнок пришёл за домашкой.
        """
        if child is None or child.consent_policy in (None, POLICY_VERSION):
            return
        try:
            await self._remind(actor, child)
        except Exception:
            logger.exception("consent renewal reminder failed")

    async def _remind(self, actor: Actor, child: StudentProfile) -> None:
        ctx = self._ctx
        if child.parent_user_id is None or not await self._due(actor, child.id):
            return
        label = child_label(child, await ctx.repo.children(child.parent_user_id))
        text, buttons = texts.renewal_text(label), texts.renewal_keyboard(child.id)
        if child.sent_by_parent:
            await ctx.reply(actor, text, buttons)  # фото прислал сам родитель
        else:
            parent = await ctx.repo.account_by_id(child.parent_user_id)
            if parent is None or parent.role != "parent":
                return
            if not await ctx.notify(
                actor, parent.user_id_enc, text, kind=NOTIFY_KIND, buttons=buttons
            ):
                return  # не дошло: родителя не спросили, в журнале — notify_failed
        ctx.log(
            "consent_renewal_asked",
            actor,
            user_initiated=False,
            from_policy=child.consent_policy,
            policy_version=POLICY_VERSION,
        )

    async def _due(self, actor: Actor, profile_id: int) -> bool:
        """Пора ли напомнить; отметка — в состоянии того, кто присылает фото."""
        ctx = self._ctx
        state = await ctx.states.get(actor.user_hash)
        now = ctx.now().timestamp()
        asked = state.renewal_asked.get(str(profile_id))
        if asked is not None and now - asked < REMIND_EVERY.total_seconds():
            return False
        marks = {**state.renewal_asked, str(profile_id): now}
        await ctx.states.set(actor.user_hash, state.model_copy(update={"renewal_asked": marks}))
        return True

    async def accept(self, actor: Actor, parent: Account, profile_id: int) -> None:
        """«Согласен» под просьбой. Чужой или устаревший `profile_id` согласия не меняет:
        владение ребёнком проверяет хранилище."""
        ctx = self._ctx
        previous = await ctx.repo.renew_consent(
            profile_id, parent.id, actor.user_hash, POLICY_VERSION, ctx.now()
        )
        if previous is None:
            return
        ctx.log("consent_renewed", actor, from_policy=previous, policy_version=POLICY_VERSION)
        await ctx.reply(actor, texts.RENEWAL_THANKS)
