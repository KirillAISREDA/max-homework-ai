"""Итоги проверки родителю и учёт проверок (спецификация онбординга §7 п.5, §9.1, §9.4).

После сводки ребёнку проверка записывается в `homeworks`, а родителю ребёнка 5–9 класса уходит
короткий итог; когда ребёнок разобрал все ошибки домашки — ещё одно сообщение. Работает только
режим «сразу»: сводки по расписанию (§9.2–9.3) пока нет.

В сообщении родителю — класс, предмет, счётчики и номера заданий с ошибкой. Текста заданий,
ответов, фото и имени в нём нет (152-ФЗ). Id MAX родителя расшифровывается только в момент
отправки (`OnboardingContext.notify`), в журнал и лог идут хэши.

Ни один сбой здесь не доходит до ребёнка: сводку он уже получил, и она не откатывается.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from hwcheck.bot.fsm import ChatState
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.pages import task_label
from hwcheck.bot.summary import lower, remaining_buttons, task_findings
from hwcheck.db.repo import Account, Homework, HomeworkCounts, NotifyMode, StudentProfile
from hwcheck.subjects.base import strength_of_task

logger = logging.getLogger(__name__)

NOTIFY_OFF_BUTTON = "Не присылать итоги"
NOTIFY_ON_BUTTON = "Присылать итоги"
SWITCHED_OFF = "Хорошо, итоги проверок больше не присылаю. Вернуть их можно кнопкой ниже."
SWITCHED_ON = "Готово! Снова буду присылать короткий итог после каждой проверки."

# «домашку по …»: названия каталога (bot/subjects.py) в дательном падеже
SUBJECT_DATIVE = {
    "math": "математике",
    "russian": "русскому языку",
    "literary_reading": "литературному чтению",
    "literature": "литературе",
    "foreign_language": "иностранному языку",
    "world_around": "окружающему миру",
    "history": "истории",
    "social_studies": "обществознанию",
    "geography": "географии",
    "biology": "биологии",
    "informatics": "информатике",
    "physics": "физике",
    "chemistry": "химии",
}


@dataclass(frozen=True)
class HomeworkSummary:
    counts: HomeworkCounts
    wrong_labels: list[str]  # «№19», «задание 2» — только задания с ошибкой


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 задание», «2 задания», «5 заданий»; 11–14 — всегда «заданий»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def summarize(state: ChatState) -> HomeworkSummary:
    """Счётчики по худшей находке задания — теми же правилами, что и сводка ребёнку."""
    strengths = [strength_of_task(task_findings(i, item)) for i, item in enumerate(state.tasks)]
    wrong = [
        lower(task_label(item.task))
        for item, strength in zip(state.tasks, strengths, strict=True)
        if strength == "verified"
    ]
    counts = HomeworkCounts(
        total=len(strengths),
        correct=strengths.count("ok"),
        wrong=len(wrong),
        uncertain=strengths.count("candidate"),
    )
    return HomeworkSummary(counts, wrong)


def child_label(child: StudentProfile, siblings: Sequence[StudentProfile]) -> str:
    """«Ребёнок (7 класс)»; дети одного класса — «Ребёнок 1 / 2» в порядке привязки, как кнопки
    «Чья домашка?» (`texts.whose_keyboard`). Имени нет: бот его не спрашивает."""
    same_grade = [sibling.id for sibling in siblings if sibling.grade == child.grade]
    if len(same_grade) > 1 and child.id in same_grade:
        return f"Ребёнок {same_grade.index(child.id) + 1} ({child.grade} класс)"
    return f"Ребёнок ({child.grade} класс)"


def _homework_of(subject: str, case: str) -> str:
    dative = SUBJECT_DATIVE.get(subject)
    return f"{case} по {dative}" if dative is not None else case


def checked_text(child: str, subject: str, summary: HomeworkSummary) -> str:
    counts = summary.counts
    tasks = plural(counts.total, "задание", "задания", "заданий")
    head = f"{child} проверил {_homework_of(subject, 'домашку')}: {tasks} — "
    single = counts.total == 1
    if counts.correct == counts.total:
        return head + ("верно ✅" if single else "все верно ✅")
    unrated = counts.total - counts.correct - counts.wrong - counts.uncertain
    # «стоит перепроверить» — не ошибка: при сомнении бот не уверен, а не обвиняет
    parts = [
        name if single else f"{n} {name}"
        for n, name in (
            (counts.correct, "верно"),
            (counts.uncertain, "стоит перепроверить"),
            (unrated, "без оценки"),
        )
        if n
    ]
    if counts.wrong:
        where = f" ({', '.join(summary.wrong_labels)})" if summary.wrong_labels else ""
        parts.append(("с ошибкой" if single else f"{counts.wrong} с ошибкой") + where)
        parts.append("разбирает с подсказками")
    return head + ", ".join(parts) + "."


def resolved_text(child: str, subject: str) -> str:
    return f"{child} разобрал все ошибки в {_homework_of(subject, 'домашке')} ✅"


def switch_keyboard(*, enabled: bool) -> Buttons:
    """Под итогом — выключатель; под подтверждением отключения — кнопка «обратно»."""
    if enabled:
        return [[callback_button(NOTIFY_OFF_BUTTON, "ob:notify:off")]]
    return [[callback_button(NOTIFY_ON_BUTTON, "ob:notify:on")]]


class ParentNotifier:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def homework_checked(
        self, chat_id: int, user_id: int | None, student_id: int | None, state: ChatState
    ) -> None:
        """Сводка ребёнку уже отправлена: запись `homeworks` и итог родителю."""
        if user_id is None or student_id is None or not state.tasks:
            return
        actor = Actor.of(chat_id, user_id)
        summary = summarize(state)
        subject = state.subject
        await self._guard(
            actor, "homework_save_failed", self._save(chat_id, student_id, subject, summary.counts)
        )
        await self._tell(
            actor,
            student_id,
            "homework_checked",
            lambda child: checked_text(child, subject, summary),
        )

    async def error_fixed(
        self, chat_id: int, user_id: int | None, state: ChatState, index: int | None
    ) -> None:
        """Разбор задания `index` дошёл до верного ответа; `state` — уже с этой отметкой."""
        if user_id is None or index is None or state.homework_id is None:
            return
        if state.resolved_indices.count(index) > 1:
            # кнопка «Разобрать» в старой сводке жива: повторный разбор — не новая ошибка
            return
        actor = Actor.of(chat_id, user_id)
        homework = await self._guard(
            actor, "homework_save_failed", self._ctx.repo.resolve_error(state.homework_id)
        )
        if homework is None or remaining_buttons(state):
            return
        # метрика «семья дошла до верного ответа»: не зависит от того, дошло ли сообщение
        self._ctx.log(
            "homework_resolved",
            actor,
            user_initiated=False,
            component="notifier",
            subject=homework.subject,
            errors=homework.errors_resolved,
        )
        subject = homework.subject
        await self._tell(
            actor,
            homework.student_id,
            "errors_resolved",
            lambda child: resolved_text(child, subject),
        )

    async def switch(self, actor: Actor, parent: Account, *, enabled: bool) -> None:
        """Кнопка под уведомлением. Режим меняется только у нажавшего: id в payload нет."""
        mode: NotifyMode = "instant" if enabled else "off"
        await self._ctx.repo.set_notify_mode(parent.id, mode)
        self._ctx.log("notify_mode_set", actor, mode=mode)
        text = SWITCHED_ON if enabled else SWITCHED_OFF
        await self._ctx.reply(actor, text, switch_keyboard(enabled=enabled))

    async def _save(
        self, chat_id: int, student_id: int, subject: str, counts: HomeworkCounts
    ) -> Homework | None:
        homework = await self._ctx.repo.add_homework(student_id, subject, counts)
        if homework is None:
            return None  # профиль удалили, пока шла проверка
        # состояние читаем заново: после сводки его мог изменить снятый уточняющий вопрос
        dialogs = self._ctx.dialogs
        state = await dialogs.get(chat_id)
        await dialogs.set(chat_id, state.model_copy(update={"homework_id": homework.id}))
        return homework

    async def _tell(
        self, actor: Actor, student_id: int, kind: str, text: Callable[[str], str]
    ) -> None:
        found = await self._guard(actor, "notify_failed", self._recipient(student_id), kind=kind)
        if found is None:
            return
        parent, child = found
        buttons = switch_keyboard(enabled=True)
        await self._ctx.notify(actor, parent.user_id_enc, text(child), kind=kind, buttons=buttons)

    async def _recipient(self, student_id: int) -> tuple[Account, str] | None:
        """Родитель, которому писать, и подпись ребёнка; None — писать некому или не нужно."""
        repo = self._ctx.repo
        child = await repo.get_profile(student_id)
        # 1–4 класс: фото присылает сам родитель и результат видит сам (§4.7 п.7)
        if child is None or child.sent_by_parent or child.parent_user_id is None:
            return None
        parent = await repo.account_by_id(child.parent_user_id)
        if parent is None or parent.role != "parent":
            return None
        # «digest» идёт как «instant»: пока нет сводки по расписанию (§9.2), иначе родитель с
        # таким режимом молча остался бы без итогов
        if await repo.notify_mode(parent.id) == "off":
            return None
        return parent, child_label(child, await repo.children(parent.id))

    async def _guard[T](
        self, actor: Actor, event: str, step: Awaitable[T], **fields: Any
    ) -> T | None:
        """Сбой базы или Redis — предупреждение и событие, а не «попробуй ещё раз» ребёнку."""
        try:
            return await step
        except Exception as exc:
            error = type(exc).__name__
            logger.warning("%s: %s", event, error)
            self._ctx.log(
                event, actor, user_initiated=False, component="notifier", error=error, **fields
            )
            return None
