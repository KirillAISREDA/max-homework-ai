"""Маршрутизатор онбординга (спецификация §4, §7): шаг выводится из данных, апдейт идёт в нужный шаг
или дальше — в сценарий проверки (bot/handlers.py).

Кнопки онбординга — `ob:<действие>[:<аргумент>]`, аргумент недоверенный. Кнопка не своего шага
(старое сообщение, чужая роль, выдуманный аргумент) показывает текущий шаг, а не действие.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from hwcheck.bot.invites import parse_code, parse_start_payload
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.linking import Linking
from hwcheck.bot.onboarding.parent import ParentSteps
from hwcheck.bot.onboarding.policy import policy_messages
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import Account, StudentProfile

Step = Literal[
    "role",  # новый пользователь
    "student_subject",  # ученик без предмета
    "waiting_parent",  # ученик без подключённого родителя
    "student_ready",
    "child_subject",  # у родителя ребёнок 1–4 без предмета
    "child_consent",  # у родителя ребёнок 1–4 без согласия
    "parent_ready",
]
_UPDATES = frozenset({"bot_started", "message_callback", "message_created"})
_GRADE = re.compile(r"[1-9]")
_PROFILE_ID = re.compile(r"[0-9]{1,18}")


@dataclass(frozen=True)
class CheckPhotos:
    """Фото, которые онбординг отдаёт в проверку (родитель выбрал, чья домашка)."""

    urls: list[str]


Route = Literal["handled", "pass"] | CheckPhotos


@dataclass(frozen=True)
class Position:
    step: Step
    account: Account | None
    profile: StudentProfile | None = None  # профиль, к которому относится шаг
    # фото/текст/кнопки проверки не блокируются: ученик готов или у родителя есть хотя бы один
    # ребёнок 1–4 класса с согласием (даже если параллельно заводится ещё один, незавершённый)
    can_check: bool = False


Action = Callable[[Actor, Position, str], Awaitable[Route | None]]


class Onboarding:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx
        self._subjects = SubjectStep(ctx)
        self._students = StudentSteps(ctx, self._subjects)
        self._parents = ParentSteps(ctx, self._subjects)
        self._linking = Linking(ctx, self._subjects)
        self._actions: dict[str, Action] = {
            "role": self._role,
            "grade": self._grade,
            "pgrade": self._parent_grade,
            "subject": self._subject,
            "consent": self._consent,
            "pconsent": self._parent_consent,
            "accept": self._accept,
            "decline": self._decline,
            "policy": self._policy,
            "example": self._example,
            "resend": self._resend,
            "addchild": self._add_child,
            "whose": self._whose,
        }

    async def route(self, update: MaxUpdate) -> Route:
        if update.update_type not in _UPDATES:
            return "pass"
        chat_id, user_id = update.effective_chat_id, update.effective_user_id
        if chat_id is None or user_id is None or update.chat_type not in (None, "dialog"):
            # без пользователя согласие не проверить, а ссылки, коды и экран согласия не должны
            # уйти в групповой чат — апдейт не должен утечь и в сценарий проверки
            if update.callback is not None and update.callback.callback_id:
                await self._ctx.max.answer_callback(update.callback.callback_id)
            return "handled"
        actor = Actor.of(chat_id, user_id)
        position = await self._position(await self._ctx.repo.get_account(actor.user_hash))
        if update.update_type == "bot_started":
            return await self._on_start(actor, position, update.payload)
        if update.callback is not None:
            payload, callback_id = update.callback.payload or "", update.callback.callback_id or ""
            return await self._on_callback(actor, position, payload, callback_id)
        if update.message is None:
            return "pass"
        if update.message.image_urls:
            return await self._on_photo(actor, position, update.message.image_urls)
        body = update.message.body
        if body is not None and body.text:
            return await self._on_text(actor, position, body.text)
        return "pass"

    async def _position(self, account: Account | None) -> Position:
        repo = self._ctx.repo
        if account is None:
            return Position("role", None)
        if account.role == "student":
            profile = await repo.own_profile(account.id)
            if profile is None:  # аккаунт ученика и профиль создаются одной транзакцией
                raise RuntimeError("student account without profile")
            if profile.subject is None:
                return Position("student_subject", account, profile)
            if profile.has_consent:
                return Position("student_ready", account, profile, can_check=True)
            return Position("waiting_parent", account, profile)
        children = await repo.children(account.id)
        # хотя бы один ребёнок 1–4 класса с согласием — проверка доступна, даже если родитель
        # параллельно заводит ещё одного (незавершённого) ребёнка
        can_check = any(c.sent_by_parent and c.has_consent for c in children)
        unfinished = [c for c in children if c.sent_by_parent and not c.has_consent]
        if unfinished:  # незавершённый ребёнок 1–4 у родителя один (start_child_by_parent)
            child = unfinished[-1]
            step: Step = "child_subject" if child.subject is None else "child_consent"
            return Position(step, account, child, can_check=can_check)
        return Position("parent_ready", account, can_check=can_check)

    async def _show(self, actor: Actor, position: Position) -> None:
        """Текущий шаг ещё раз: ответ на текст, фото, старую или чужую кнопку."""
        profile, account = position.profile, position.account
        if position.step == "role":
            await self._ctx.reply(actor, texts.HELLO, texts.role_keyboard())
        elif position.step in ("student_subject", "child_subject") and profile is not None:
            await self._subjects.ask(actor, profile)
        elif position.step == "waiting_parent":
            await self._students.remind_waiting(actor)
        elif position.step == "student_ready":
            await self._ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        elif position.step == "child_consent":
            await self._parents.ask_consent(actor)
        elif position.step == "parent_ready" and account is not None:
            await self._parents.show_status(actor, account)

    async def _on_start(self, actor: Actor, position: Position, payload: str | None) -> Route:
        invite = parse_start_payload(payload)
        self._ctx.log("bot_started", actor, invite=invite is not None)
        if invite is None:
            await self._show(actor, position)
        else:
            kind, token = invite
            await self._linking.open_link(actor, position.account, kind, token)
        return "handled"

    async def _on_text(self, actor: Actor, position: Position, message: str) -> Route:
        if position.step == "student_ready":
            return "pass"  # похожий на код текст тоже: у ученика с родителем код уже не нужен
        if position.can_check:
            dialog = await self._ctx.dialogs.get(actor.chat_id)
            if dialog.phase != "idle":
                # родитель отвечает на уточнение или в разборе ошибки — даже похожим на код текстом
                return "pass"
        code = parse_code(message)
        if code is not None:
            await self._linking.open_code(actor, position.account, code)
            return "handled"
        await self._show(actor, position)
        return "handled"

    async def _on_photo(self, actor: Actor, position: Position, urls: list[str]) -> Route:
        if position.step == "student_ready":
            return "pass"
        account = position.account
        if account is not None and (position.step == "parent_ready" or position.can_check):
            chosen = await self._parents.on_photo(actor, account, urls)
            return CheckPhotos(chosen) if chosen else "handled"
        self._ctx.log("photo_blocked_no_consent", actor, step=position.step)
        if position.step == "waiting_parent":
            await self._students.block_photo(actor)
        else:
            await self._show(actor, position)
        return "handled"

    async def _on_callback(
        self, actor: Actor, position: Position, payload: str, callback_id: str
    ) -> Route:
        if not payload.startswith("ob:") and position.can_check:
            return "pass"
        await self._ctx.max.answer_callback(callback_id)
        if not payload.startswith("ob:"):  # кнопка проверки у того, кто онбординг не прошёл
            await self._show(actor, position)
            return "handled"
        self._ctx.log("button_pressed", actor, payload=payload)
        name, _, arg = payload.removeprefix("ob:").partition(":")
        action = self._actions.get(name)
        route = await action(actor, position, arg) if action is not None else None
        if route is None:
            await self._show(actor, position)
            return "handled"
        return route

    # --- действия кнопок: None — кнопка не для этого шага, маршрутизатор покажет текущий ---

    async def _role(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "role" or arg not in ("student", "parent"):
            return None
        self._ctx.log("onboarding_role_chosen", actor, role=arg)
        if arg == "student":
            await self._ctx.reply(actor, texts.STUDENT_GRADE, texts.grade_keyboard("grade"))
        else:
            await self._parents.ask_grade(actor)
        return "handled"

    async def _grade(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "role" or not _GRADE.fullmatch(arg):
            return None
        await self._students.choose_grade(actor, int(arg))
        return "handled"

    async def _parent_grade(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position) or not _GRADE.fullmatch(arg):
            return None
        await self._parents.choose_grade(actor, position.account, int(arg))
        return "handled"

    async def _subject(self, actor: Actor, position: Position, arg: str) -> Route | None:
        profile = position.profile
        if position.step not in ("student_subject", "child_subject") or profile is None:
            return None
        if await self._subjects.choose(actor, profile, arg):
            if position.step == "student_subject":
                await self._students.after_subject(actor, replace(profile, subject=arg))
            else:
                await self._parents.ask_consent(actor)
        return "handled"

    async def _consent(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "child_consent" or position.profile is None:
            return None
        await self._parents.give_consent(actor, position.profile)
        return "handled"

    async def _parent_consent(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position) or not _GRADE.fullmatch(arg):
            return None
        if int(arg) <= PARENT_SENDS_UP_TO_GRADE:  # за 1–4 класс ссылка ребёнку не создаётся
            return None
        await self._parents.invite_child(actor, position.account, int(arg))
        return "handled"

    async def _accept(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._linking.accept(actor, arg)  # arg — метка показанной ссылки
        return "handled"

    async def _decline(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._linking.decline(actor, arg)
        return "handled"

    async def _policy(self, actor: Actor, position: Position, arg: str) -> Route | None:
        for message in policy_messages():
            await self._ctx.reply(actor, message)
        return "handled"

    async def _example(self, actor: Actor, position: Position, arg: str) -> Route | None:
        await self._ctx.reply(actor, texts.EXAMPLE)
        return "handled"

    async def _resend(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "waiting_parent" or position.profile is None:
            return None
        await self._students.invite_parent(actor, position.profile)
        return "handled"

    async def _add_child(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._parents.ask_grade(actor)
        return "handled"

    async def _whose(self, actor: Actor, position: Position, arg: str) -> Route | None:
        # доступно и при незавершённом ребёнке, как фото; владение и согласие проверяет choose_owner
        account = position.account
        if account is None or account.role != "parent" or not position.can_check:
            return None
        if not _PROFILE_ID.fullmatch(arg):
            return None
        urls = await self._parents.choose_owner(actor, account, int(arg))
        return CheckPhotos(urls) if urls else "handled"


def _parent_side(position: Position) -> bool:
    """Действие родителя доступно новому пользователю и родителю, но не ученику."""
    return position.account is None or position.account.role == "parent"
