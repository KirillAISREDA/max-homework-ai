"""Текст отчёта родителю о проверках за период — чистые функции, без базы и сети. Отчёт по
запросу и отчёт раз в неделю собираются из одних и тех же блоков детей.

Отчёт собирается кодом из счётчиков, модель в нём не участвует. В тексте — класс, предмет и
числа: ни текста заданий, ни ответов, ни фото, ни имени (152-ФЗ). Детей между собой отчёт не
сравнивает: рейтингов, серий и призов в нём нет.

Обычный текст без разметки: `send_to_user` разметку не передаёт, а markdown в MAX живьём не
проверен — звёздочки в отчёте хуже, чем отчёт без жирного шрифта.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.notifier import plural
from hwcheck.bot.onboarding.context import MSK
from hwcheck.bot.subjects import subject_by_code
from hwcheck.bot.summary import MAX_MESSAGE_CHARS, message_length
from hwcheck.db.reports import SubjectTotals

REPORT_DAYS = 7
HEADER = "📈 Отчёт за 7 дней: {period}"
NO_CHECKS = "Проверок за эти дни не было."
# за ребёнка 1–4 класса фото присылает сам родитель: «ребёнок пришлёт» было бы неправдой
WAITING_FOR_CHILD = "Когда ребёнок пришлёт домашку, здесь появятся задания, ошибки и разборы."
WAITING_FOR_PARENT = "Когда вы пришлёте фото домашки, здесь появятся задания, ошибки и разборы."
HIDDEN_CHILDREN = "Не показано детей: {n} — отчёт не поместился в сообщение."
MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip

# отчёт раз в неделю (bot/report_schedule.py): неделя — 7 суток до часа рассылки
WEEKLY_HEADER = "📈 Отчёт за неделю: {period}"
WEEKLY_EMPTY = "На этой неделе домашку на проверку не присылали."
# «присылать по воскресеньям»: день рассылки задаёт настройка, по индексу — с понедельника
WEEKDAYS_DATIVE = (
    "по понедельникам", "по вторникам", "по средам", "по четвергам",
    "по пятницам", "по субботам", "по воскресеньям",
)  # fmt: skip
WEEKLY_SWITCHED_OFF = (
    "Хорошо, отчёт {days} больше не присылаю. Отчёт по кнопке «📈 Отчёт о прогрессе» остаётся. "
    "Вернуть рассылку можно кнопкой ниже."
)
WEEKLY_SWITCHED_ON = "Готово! Снова буду присылать отчёт о прогрессе {days}."


@dataclass(frozen=True)
class Period:
    start: datetime  # UTC, входит в период
    end: datetime  # UTC, в период не входит
    first_day: date  # подпись периода — дни по московскому времени
    last_day: date


@dataclass(frozen=True)
class ChildReport:
    label: str  # «Ребёнок (7 класс)»: имени бот не знает
    subjects: Sequence[SubjectTotals]  # пусто — проверок за период не было


def request_period(now: datetime) -> Period:
    """Последние 7 календарных дней по московскому времени, включая сегодня.

    Часовой пояс семьи неизвестен, поэтому день считается по Москве — как учебный год."""
    today = now.astimezone(MSK).date()
    first_day = today - timedelta(days=REPORT_DAYS - 1)
    start = datetime.combine(first_day, time.min, tzinfo=MSK)
    return Period(
        start=start.astimezone(UTC),
        end=(start + timedelta(days=REPORT_DAYS)).astimezone(UTC),
        first_day=first_day,
        last_day=today,
    )


def period_label(first: date, last: date) -> str:
    """«23–29 сентября», «29 сентября – 5 октября», «29 декабря 2026 – 4 января 2027»."""
    first_month, last_month = MONTHS_GENITIVE[first.month - 1], MONTHS_GENITIVE[last.month - 1]
    if first.year != last.year:
        return f"{first.day} {first_month} {first.year} – {last.day} {last_month} {last.year}"
    if first.month != last.month:
        return f"{first.day} {first_month} – {last.day} {last_month}"
    if first.day != last.day:
        return f"{first.day}–{last.day} {last_month}"
    return f"{last.day} {last_month}"


def _subject_title(code: str) -> str:
    subject = subject_by_code(code)
    return subject.title if subject is not None else code


def _subject_lines(totals: SubjectTotals) -> list[str]:
    counts = totals.counts
    homeworks = plural(totals.homeworks, "домашка", "домашки", "домашек")
    tasks = plural(counts.total, "задание", "задания", "заданий")
    head = f"{_subject_title(totals.subject)} — {homeworks}, {tasks}"
    if counts.total > 0 and counts.correct == counts.total:
        return [f"{head}: {'верно' if counts.total == 1 else 'все верно'} ✅"]
    # повторный разбор той же ошибки считается ещё раз: разборов бывает больше, чем ошибок
    resolved = min(totals.errors_resolved, counts.wrong)
    wrong = f"с ошибкой — {counts.wrong}"
    if resolved > 0:
        wrong += f", из них разобрал с подсказками — {resolved}"
    # «без оценки» своей колонки не имеет — это остаток, как в итоге после проверки
    unrated = counts.total - counts.correct - counts.wrong - counts.uncertain
    # «стоит перепроверить» — не ошибка: при сомнении бот не уверен, а не обвиняет
    lines = [
        (counts.correct, f"верно — {counts.correct}"),
        (counts.wrong, wrong),
        (counts.uncertain, f"стоит перепроверить — {counts.uncertain}"),
        (unrated, f"без оценки — {unrated}"),
    ]
    shown = [f"• {line}" for n, line in lines if n > 0]
    return [f"{head}:", *shown] if shown else [head]


def _child_block(child: ChildReport) -> str:
    if not child.subjects:
        return f"{child.label}\n{NO_CHECKS}"
    lines = [line for totals in child.subjects for line in _subject_lines(totals)]
    return "\n".join([child.label, *lines])


def _fit(header: str, blocks: Sequence[str]) -> str:
    """Отчёт — одно сообщение MAX: что не поместилось, отбрасывается целыми блоками детей, и
    родитель видит, о скольких детях отчёта нет."""
    shown = len(blocks)
    while True:
        parts = [header, *blocks[:shown]]
        if shown < len(blocks):
            parts.append(HIDDEN_CHILDREN.format(n=len(blocks) - shown))
        text = "\n\n".join(parts)
        # длина — как в сводке проверки: эмодзи занимает два элемента UTF-16
        if message_length(text) <= MAX_MESSAGE_CHARS or shown == 0:
            return text
        shown -= 1


def render_report(period: Period, children: Sequence[ChildReport], *, sends_himself: bool) -> str:
    """`children` — дети с действующим согласием, по порядку добавления. `sends_himself` — все
    дети родителя в 1–4 классе: фото домашки он присылает сам."""
    header = HEADER.format(period=period_label(period.first_day, period.last_day))
    if not any(child.subjects for child in children):
        waiting = WAITING_FOR_PARENT if sends_himself else WAITING_FOR_CHILD
        return f"{header}\n\n{NO_CHECKS} {waiting}"
    return _fit(header, [_child_block(child) for child in children])


def render_weekly(period: Period, children: Sequence[ChildReport]) -> str:
    """Отчёт раз в неделю: свой заголовок и те же блоки детей, что по запросу. Домашек за
    неделю не было — одна строка: отчёта родитель не просил, и объяснять ему, как пользоваться
    ботом, незачем. Слать ли такую строку вообще, решает рассылка (bot/report.py)."""
    header = WEEKLY_HEADER.format(period=period_label(period.first_day, period.last_day))
    if not any(child.subjects for child in children):
        return f"{header}\n\n{WEEKLY_EMPTY}"
    return _fit(header, [_child_block(child) for child in children])


def weekly_keyboard(weekday: int, *, enabled: bool) -> Buttons:
    """Под отчётом раз в неделю — выключатель; под подтверждением отключения — кнопка
    «обратно». Аргумент payload — только `on` / `off`: выключатель меняет нажавший и себе."""
    days = WEEKDAYS_DATIVE[weekday]
    if enabled:
        return [[callback_button(f"Не присылать {days}", "ob:weekly:off")]]
    return [[callback_button(f"Присылать {days}", "ob:weekly:on")]]


def weekly_switched(weekday: int, *, enabled: bool) -> str:
    text = WEEKLY_SWITCHED_ON if enabled else WEEKLY_SWITCHED_OFF
    return text.format(days=WEEKDAYS_DATIVE[weekday])
