"""Отчёт родителю о проверках за 7 дней — по кнопке «📈 Отчёт о прогрессе».

Отчёт — всегда о детях нажавшего: в payload кнопки нет ничьих id, а детей хранилище ищет по
аккаунту родителя. В отчёт идут только дети с действующим согласием. Рассылки по расписанию
здесь нет — она появится отдельно.

Повторное нажатие раньше паузы отчёт не собирает: кнопку легко нажать дважды, а можно жать
и без остановки — каждое нажатие стоило бы запроса к базе и сообщения (ревью 29.09).

Сбой сборки или отправки не становится сбоем апдейта: родитель видит короткое извинение, в
журнале — `parent_report_failed`. В журнал идут счётчики и обезличенный id нажавшего: ни id
профилей, ни id MAX, ни текста исключения (в нём бывают данные запроса).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hwcheck.bot.notifier import child_label
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.report_text import ChildReport, render_report, request_period
from hwcheck.db.repo import Account
from hwcheck.db.reports import ReportRepository, SubjectTotals

logger = logging.getLogger(__name__)

ON_REQUEST = "on_request"
# за это время данные отчёта почти не меняются, а отправленный отчёт — выше в чате
REPORT_COOLDOWN = timedelta(seconds=20)
REPORT_FAILED = "Не получилось собрать отчёт 😔 Попробуйте ещё раз чуть позже."


@dataclass(frozen=True)
class _Report:
    text: str
    children: int  # детей в отчёте
    homeworks: int  # проверок в отчёте


class ParentReporter:
    def __init__(self, ctx: OnboardingContext, reports: ReportRepository) -> None:
        self._ctx = ctx
        self._reports = reports
        # кому отчёт ушёл недавно: хэш родителя → когда. В памяти процесса: после рестарта
        # пауза начинается заново, и это не страшно
        self._recent: dict[str, datetime] = {}

    async def on_request(self, actor: Actor, parent: Account) -> None:
        """Кнопка отчёта: ответ — в тот же чат, где она нажата."""
        if self._too_soon(actor):
            self._log("parent_report_skipped", actor, reason="cooldown")
            return
        try:
            report = await self._build(parent)
            await self._ctx.reply(actor, report.text)
        except Exception as exc:
            await self._failed(actor, exc)
            return
        # пауза — только после отправленного отчёта: после сбоя родитель пробует снова
        self._recent[actor.user_hash] = self._ctx.now()
        self._log(
            "parent_report_sent",
            actor,
            children=report.children,
            homeworks=report.homeworks,
            empty=report.homeworks == 0,
        )

    def _too_soon(self, actor: Actor) -> bool:
        now = self._ctx.now()
        # отметки старше паузы не нужны: словарь не растёт с числом родителей
        self._recent = {
            user: sent for user, sent in self._recent.items() if now - sent < REPORT_COOLDOWN
        }
        return actor.user_hash in self._recent

    async def _build(self, parent: Account) -> _Report:
        family = await self._ctx.repo.children(parent.id)
        children = [child for child in family if child.has_consent]
        period = request_period(self._ctx.now())
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
        text = render_report(period, blocks, sends_himself=sends_himself)
        homeworks = sum(totals.homeworks for block in blocks for totals in block.subjects)
        return _Report(text, len(children), homeworks)

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
