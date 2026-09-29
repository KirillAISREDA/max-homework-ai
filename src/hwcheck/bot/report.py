"""Отчёт родителю о проверках: по кнопке «📈 Отчёт о прогрессе» — за 7 дней, и раз в неделю
по расписанию (bot/report_schedule.py) — за неделю до часа рассылки.

Отчёт — всегда о детях своего получателя: в payload кнопки нет ничьих id, а детей хранилище
ищет по аккаунту родителя. В отчёт идут только дети с действующим согласием.

Повторное нажатие раньше паузы отчёт не собирает: кнопку легко нажать дважды, а можно жать
и без остановки — каждое нажатие стоило бы запроса к базе и сообщения (ревью 29.09).

Отчёт раз в неделю никто не нажимал, и чата для ответа нет: он уходит родителю по его id MAX,
который расшифровывается в момент отправки (`OnboardingContext.send_to`). Неделя без домашек —
одно короткое сообщение, и только если неделей раньше домашки были: семья, которая ботом не
пользуется, «пустой» отчёт получает не больше одного раза.

Сбой сборки или отправки не становится сбоем апдейта или рассылки: по кнопке родитель видит
короткое извинение, в журнале — `parent_report_failed`. В журнал идут счётчики и обезличенный
id получателя: ни id профилей, ни id MAX, ни текста исключения (в нём бывают данные запроса).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hwcheck.bot.notifier import child_label
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.report_schedule import WeeklySchedule, previous_period, weekly_period
from hwcheck.bot.report_text import (
    ChildReport,
    Period,
    render_report,
    render_weekly,
    request_period,
    weekly_keyboard,
    weekly_switched,
)
from hwcheck.db.repo import Account
from hwcheck.db.reports import ReportRepository, SubjectTotals, WeeklyStatus

logger = logging.getLogger(__name__)

ON_REQUEST = "on_request"
WEEKLY = "weekly"
# за это время данные отчёта почти не меняются, а отправленный отчёт — выше в чате
REPORT_COOLDOWN = timedelta(seconds=20)
REPORT_FAILED = "Не получилось собрать отчёт 😔 Попробуйте ещё раз чуть позже."


@dataclass(frozen=True)
class _Family:
    """Дети родителя с действующим согласием и их проверки за период."""

    blocks: list[ChildReport]
    ids: frozenset[int]  # профили этих детей: в тексты и журнал не идут
    sends_himself: bool  # все дети в 1–4 классе: фото домашки родитель присылает сам

    @property
    def children(self) -> int:
        return len(self.blocks)

    @property
    def homeworks(self) -> int:
        return sum(totals.homeworks for block in self.blocks for totals in block.subjects)


class ParentReporter:
    def __init__(
        self,
        ctx: OnboardingContext,
        reports: ReportRepository,
        schedule: WeeklySchedule | None = None,
    ) -> None:
        self._ctx = ctx
        self._reports = reports
        # день рассылки — в надписях выключателя: «Не присылать по воскресеньям»
        self._schedule = schedule or WeeklySchedule()
        # кому отчёт ушёл недавно: хэш родителя → когда. В памяти процесса: после рестарта
        # пауза начинается заново, и это не страшно
        self._recent: dict[str, datetime] = {}

    async def on_request(self, actor: Actor, parent: Account) -> None:
        """Кнопка отчёта: ответ — в тот же чат, где она нажата."""
        if self._too_soon(actor):
            self._log("parent_report_skipped", actor, reason="cooldown")
            return
        try:
            period = request_period(self._ctx.now())
            family = await self._collect(parent, period)
            text = render_report(period, family.blocks, sends_himself=family.sends_himself)
            await self._ctx.reply(actor, text)
        except Exception as exc:
            await self._failed(actor, exc)
            return
        # пауза — только после отправленного отчёта: после сбоя родитель пробует снова
        self._recent[actor.user_hash] = self._ctx.now()
        self._log(
            "parent_report_sent",
            actor,
            children=family.children,
            homeworks=family.homeworks,
            empty=family.homeworks == 0,
        )

    async def send_weekly(self, parent: Account, slot: datetime) -> WeeklyStatus:
        """Отчёт недели, кончившейся в `slot`. Любой сбой — `failed` и событие, а не
        исключение: рассылка идёт дальше, к следующему родителю."""
        try:
            period = weekly_period(slot)
            family = await self._collect(parent, period)
            reason = await self._nothing_to_send(parent, family, slot)
            if reason is not None:
                self._log_weekly("parent_report_skipped", parent, reason=reason)
                return "skipped"
            buttons = weekly_keyboard(self._schedule.weekday, enabled=True)
            text = render_weekly(period, family.blocks)
            await self._ctx.send_to(parent.user_id_enc, text, buttons=buttons)
        except Exception as exc:
            return self.weekly_failed(parent, exc)
        self._log_weekly(
            "parent_report_sent",
            parent,
            children=family.children,
            homeworks=family.homeworks,
            empty=family.homeworks == 0,
        )
        return "sent"

    def weekly_failed(self, parent: Account, exc: Exception) -> WeeklyStatus:
        """Отчёт не собран или не отправлен. Зовёт и рассылка: срок отправки истёк."""
        error = type(exc).__name__
        logger.warning("weekly report failed: %s", error)
        self._log_weekly("parent_report_failed", parent, error=error)
        return "failed"

    async def switch_weekly(self, actor: Actor, parent: Account, *, enabled: bool) -> None:
        """Кнопка под отчётом раз в неделю. Выключатель меняется только у нажавшего: id в
        payload нет. Итоги после проверки (`notify_mode`) и отчёт по кнопке он не трогает."""
        await self._reports.set_weekly(parent.id, enabled)
        self._ctx.log("weekly_report_set", actor, component="report", enabled=enabled)
        weekday = self._schedule.weekday
        text = weekly_switched(weekday, enabled=enabled)
        await self._ctx.reply(actor, text, weekly_keyboard(weekday, enabled=enabled))

    def _too_soon(self, actor: Actor) -> bool:
        now = self._ctx.now()
        # отметки старше паузы не нужны: словарь не растёт с числом родителей
        self._recent = {
            user: sent for user, sent in self._recent.items() if now - sent < REPORT_COOLDOWN
        }
        return actor.user_hash in self._recent

    async def _collect(self, parent: Account, period: Period) -> _Family:
        family = await self._ctx.repo.children(parent.id)
        children = [child for child in family if child.has_consent]
        by_child: dict[int, list[SubjectTotals]] = {}
        for totals in await self._reports.totals(parent.id, period.start, period.end):
            by_child.setdefault(totals.student_id, []).append(totals)
        # подпись — по всем детям семьи, как в итоге после проверки: «Ребёнок 2» там и здесь —
        # один и тот же ребёнок
        blocks = [
            ChildReport(child_label(child, family), by_child.get(child.id, []))
            for child in children
        ]
        sends_himself = bool(children) and all(child.sent_by_parent for child in children)
        return _Family(blocks, frozenset(child.id for child in children), sends_himself)

    async def _nothing_to_send(
        self, parent: Account, family: _Family, slot: datetime
    ) -> str | None:
        """Причина не слать отчёт недели; None — слать."""
        if family.children == 0:
            return "no_children"  # согласие отозвано после выбора получателей
        if family.homeworks > 0:
            return None
        before = previous_period(slot)
        totals = await self._reports.totals(parent.id, before.start, before.end)
        # домашки ребёнка, чьё согласие отозвано, — не повод писать родителю
        if any(row.student_id in family.ids for row in totals):
            return None  # первая неделя без домашек: одно короткое сообщение
        return "empty"

    async def _failed(self, actor: Actor, exc: Exception) -> None:
        error = type(exc).__name__
        logger.warning("parent report failed: %s", error)
        self._log("parent_report_failed", actor, error=error)
        # не дошёл отчёт — может не дойти и извинение (MAX недоступен): событие уже записано
        with contextlib.suppress(Exception):
            await self._ctx.reply(actor, REPORT_FAILED)

    def _log(self, event: str, actor: Actor, **fields: Any) -> None:
        try:
            self._ctx.log(event, actor, component="report", kind=ON_REQUEST, **fields)
        except Exception as exc:
            # журнал тоже может отказать (диск): родителю это не сбой, в логе — предупреждение
            logger.warning("%s not written: %s", event, type(exc).__name__)

    def _log_weekly(self, event: str, parent: Account, **fields: Any) -> None:
        try:
            self._ctx.log_account(event, parent, component="report", kind=WEEKLY, **fields)
        except Exception as exc:
            # отказал журнал или id не расшифровался: отправленный отчёт от этого не меняется
            logger.warning("%s not written: %s", event, type(exc).__name__)
