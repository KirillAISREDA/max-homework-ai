"""Текст отчёта родителю — по запросу (за 7 дней) и раз в неделю: период по московскому
времени, блоки детей, счётчики, выключатель рассылки.

В отчёте — класс, предмет и счётчики: ни текста заданий, ни ответов, ни фото, ни имени. Сравнений
детей между собой, рейтингов и призов в нём нет.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from hwcheck.bot.report_text import (
    ChildReport,
    Period,
    period_label,
    render_report,
    render_weekly,
    request_period,
    weekly_keyboard,
    weekly_switched,
)
from hwcheck.bot.summary import MAX_MESSAGE_CHARS, message_length
from hwcheck.db.repo import HomeworkCounts
from hwcheck.db.reports import SubjectTotals

WEEK = request_period(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
HEADER = "📈 Отчёт за 7 дней: 23–29 сентября"


def totals(
    homeworks: int,
    total: int,
    correct: int,
    wrong: int = 0,
    uncertain: int = 0,
    *,
    resolved: int = 0,
    subject: str = "math",
) -> SubjectTotals:
    counts = HomeworkCounts(total=total, correct=correct, wrong=wrong, uncertain=uncertain)
    return SubjectTotals(1, subject, homeworks, counts, errors_resolved=resolved)


def child(label: str, *subjects: SubjectTotals) -> ChildReport:
    return ChildReport(label, list(subjects))


def one_child(*subjects: SubjectTotals) -> str:
    """Отчёт об одном ребёнке без заголовка и подписи — только строки предметов."""
    text = render_report(WEEK, [child("Ребёнок (7 класс)", *subjects)], sends_himself=False)
    assert text.startswith(f"{HEADER}\n\nРебёнок (7 класс)\n")
    return text.removeprefix(f"{HEADER}\n\nРебёнок (7 класс)\n")


# --- период ---


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        (date(2026, 9, 23), date(2026, 9, 29), "23–29 сентября"),
        (date(2026, 10, 1), date(2026, 10, 7), "1–7 октября"),
        (date(2026, 9, 29), date(2026, 10, 5), "29 сентября – 5 октября"),
        (date(2026, 12, 29), date(2027, 1, 4), "29 декабря 2026 – 4 января 2027"),
        (date(2026, 9, 29), date(2026, 9, 29), "29 сентября"),
    ],
)
def test_period_label(first: date, last: date, expected: str) -> None:
    assert period_label(first, last) == expected


def test_period_label_names_every_month_in_genitive() -> None:
    names = [period_label(date(2026, m, 1), date(2026, m, 7)) for m in range(1, 13)]
    assert names == [
        "1–7 января", "1–7 февраля", "1–7 марта", "1–7 апреля", "1–7 мая", "1–7 июня",
        "1–7 июля", "1–7 августа", "1–7 сентября", "1–7 октября", "1–7 ноября", "1–7 декабря",
    ]  # fmt: skip


def test_request_period_is_seven_moscow_days_including_today() -> None:
    period = request_period(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    assert period == Period(
        start=datetime(2026, 9, 22, 21, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 21, 0, tzinfo=UTC),
        first_day=date(2026, 9, 23),
        last_day=date(2026, 9, 29),
    )
    assert period.end - period.start == timedelta(days=7)


def test_request_period_changes_at_moscow_midnight() -> None:
    """20:59 UTC — в Москве ещё 29 сентября, 21:00 UTC — уже 30-е."""
    before = request_period(datetime(2026, 9, 29, 20, 59, tzinfo=UTC))
    assert (before.first_day, before.last_day) == (date(2026, 9, 23), date(2026, 9, 29))
    assert before.end == datetime(2026, 9, 29, 21, 0, tzinfo=UTC)

    after = request_period(datetime(2026, 9, 29, 21, 0, tzinfo=UTC))
    assert (after.first_day, after.last_day) == (date(2026, 9, 24), date(2026, 9, 30))
    assert after.start == datetime(2026, 9, 23, 21, 0, tzinfo=UTC)
    assert after.end == datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 9, 29, 20, 59, tzinfo=UTC),
        datetime(2026, 9, 29, 21, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 30, tzinfo=UTC),
    ],
)
def test_request_period_contains_the_moment_of_request(now: datetime) -> None:
    period = request_period(now)
    assert period.start <= now < period.end
    assert period.start.tzinfo is not None and period.start.utcoffset() == timedelta(0)


def test_request_period_does_not_depend_on_timezone_of_the_clock() -> None:
    vladivostok = timezone(timedelta(hours=10))
    now = datetime(2026, 9, 29, 20, 59, tzinfo=UTC)
    assert request_period(now.astimezone(vladivostok)) == request_period(now)


# --- тексты ---


def test_week_with_homework_and_errors() -> None:
    children = [
        child("Ребёнок (7 класс)", totals(4, 23, 18, 3, 2, resolved=2)),
        child("Ребёнок (3 класс)", totals(2, 9, 9)),
    ]
    assert render_report(WEEK, children, sends_himself=False) == (
        "📈 Отчёт за 7 дней: 23–29 сентября\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 4 домашки, 23 задания:\n"
        "• верно — 18\n"
        "• с ошибкой — 3, из них разобрал с подсказками — 2\n"
        "• стоит перепроверить — 2\n"
        "\n"
        "Ребёнок (3 класс)\n"
        "Математика — 2 домашки, 9 заданий: все верно ✅"
    )


def test_everything_correct() -> None:
    assert one_child(totals(3, 14, 14)) == "Математика — 3 домашки, 14 заданий: все верно ✅"
    assert one_child(totals(1, 1, 1)) == "Математика — 1 домашка, 1 задание: верно ✅"


def test_no_checks_at_all() -> None:
    expected = (
        "📈 Отчёт за 7 дней: 23–29 сентября\n"
        "\n"
        "Проверок за эти дни не было. Когда ребёнок пришлёт домашку, здесь появятся задания, "
        "ошибки и разборы."
    )
    assert render_report(WEEK, [child("Ребёнок (7 класс)")], sends_himself=False) == expected
    assert render_report(WEEK, [], sends_himself=False) == expected


def test_no_checks_for_parent_who_sends_photos_himself() -> None:
    text = render_report(WEEK, [child("Ребёнок (2 класс)")], sends_himself=True)
    assert text == (
        "📈 Отчёт за 7 дней: 23–29 сентября\n"
        "\n"
        "Проверок за эти дни не было. Когда вы пришлёте фото домашки, здесь появятся задания, "
        "ошибки и разборы."
    )


def test_child_without_homework_among_children_with_homework() -> None:
    children = [
        child("Ребёнок (7 класс)", totals(1, 2, 2)),
        child("Ребёнок (5 класс)"),
    ]
    assert render_report(WEEK, children, sends_himself=False) == (
        "📈 Отчёт за 7 дней: 23–29 сентября\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 1 домашка, 2 задания: все верно ✅\n"
        "\n"
        "Ребёнок (5 класс)\n"
        "Проверок за эти дни не было."
    )


@pytest.mark.parametrize(
    ("n", "homeworks", "tasks"),
    [
        (1, "1 домашка", "1 задание"),
        (2, "2 домашки", "2 задания"),
        (5, "5 домашек", "5 заданий"),
        (11, "11 домашек", "11 заданий"),
        (12, "12 домашек", "12 заданий"),
        (13, "13 домашек", "13 заданий"),
        (14, "14 домашек", "14 заданий"),
        (21, "21 домашка", "21 задание"),
    ],
)
def test_plural_forms(n: int, homeworks: str, tasks: str) -> None:
    praise = "верно ✅" if n == 1 else "все верно ✅"
    assert one_child(totals(n, n, n)) == f"Математика — {homeworks}, {tasks}: {praise}"


def test_lines_with_zero_are_left_out() -> None:
    assert one_child(totals(1, 5, 4, 1)) == (
        "Математика — 1 домашка, 5 заданий:\n• верно — 4\n• с ошибкой — 1"
    )
    assert one_child(totals(1, 3, 0, 0, 3)) == (
        "Математика — 1 домашка, 3 задания:\n• стоит перепроверить — 3"
    )


def test_tasks_without_grade_are_the_rest_of_total() -> None:
    """«Без оценки» — остаток от общего числа, как в итоге после проверки (`checked_text`)."""
    assert one_child(totals(2, 10, 6, 1, 1)) == (
        "Математика — 2 домашки, 10 заданий:\n"
        "• верно — 6\n"
        "• с ошибкой — 1\n"
        "• стоит перепроверить — 1\n"
        "• без оценки — 2"
    )
    assert one_child(totals(1, 1, 0)) == "Математика — 1 домашка, 1 задание:\n• без оценки — 1"


def test_resolved_errors_are_capped_at_errors() -> None:
    """Повторный разбор той же ошибки считается ещё раз: разборов бывает больше, чем ошибок."""
    assert one_child(totals(1, 4, 2, 2, resolved=5)) == (
        "Математика — 1 домашка, 4 задания:\n"
        "• верно — 2\n"
        "• с ошибкой — 2, из них разобрал с подсказками — 2"
    )
    # разбор без ошибки (после «стоит перепроверить») строки об ошибках не создаёт
    assert "с ошибкой" not in one_child(totals(1, 4, 2, 0, 2, resolved=1))


def test_doubt_is_never_called_an_error() -> None:
    text = one_child(totals(1, 3, 2, 0, 1))
    assert "стоит перепроверить — 1" in text
    assert "ошиб" not in text


def test_subjects_are_named_by_catalogue_and_unknown_code_is_shown_as_is() -> None:
    text = one_child(totals(1, 2, 2, subject="russian"), totals(1, 1, 1, subject="astronomy"))
    assert text == (
        "Русский язык — 1 домашка, 2 задания: все верно ✅\n"
        "astronomy — 1 домашка, 1 задание: верно ✅"
    )


def test_report_has_no_markup_ratings_or_prizes() -> None:
    children = [
        child("Ребёнок 1 (7 класс)", totals(4, 23, 18, 3, 2, resolved=2)),
        child("Ребёнок 2 (7 класс)", totals(2, 9, 9)),
    ]
    text = render_report(WEEK, children, sends_himself=False).lower()
    for mark in ("**", "__", "`", "<b>", "#"):
        assert mark not in text
    for word in ("рейтинг", "лучше", "хуже", "приз", "подряд", "место"):
        assert word not in text


# --- длина сообщения ---


def busy_child(number: int) -> ChildReport:
    subjects = [
        totals(11, 120, 80, 20, 10, resolved=15, subject=code)
        for code in ("math", "russian", "literature", "foreign_language", "history", "biology")
    ]
    return child(f"Ребёнок {number} (7 класс)", *subjects)


def test_ten_children_fit_into_one_message() -> None:
    children = [child(f"Ребёнок {n} (7 класс)", totals(4, 23, 18, 3, 2)) for n in range(1, 11)]
    text = render_report(WEEK, children, sends_himself=False)
    assert len(text) < 4000
    assert all(item.label in text for item in children)
    assert "Не показано" not in text


def test_long_report_is_cut_by_children_and_says_how_many_are_hidden() -> None:
    children = [busy_child(n) for n in range(1, 11)]
    text = render_report(WEEK, children, sends_himself=False)
    assert len(text) < 4000
    shown = [item for item in children if f"{item.label}\n" in text]
    assert 0 < len(shown) < 10
    assert shown == children[: len(shown)]  # показаны первые по порядку, блок — целиком
    assert text.endswith(f"Не показано детей: {10 - len(shown)} — отчёт не поместился в сообщение.")


def test_report_length_is_counted_the_way_the_bot_counts_other_messages() -> None:
    """Эмодзи в UTF-16 занимает два элемента: длина считается как в сводке проверки, иначе
    отчёт с эмодзи прошёл бы свою проверку и не прошёл бы предел MAX."""
    children = [busy_child(n) for n in range(1, 11)]
    text = render_report(WEEK, children, sends_himself=False)
    assert len(text) < message_length(text) <= MAX_MESSAGE_CHARS


# --- отчёт раз в неделю ---

# неделя до воскресенья 04.10.2026 18:00 мск
SUNDAY_WEEK = Period(
    start=datetime(2026, 9, 27, 15, 0, tzinfo=UTC),
    end=datetime(2026, 10, 4, 15, 0, tzinfo=UTC),
    first_day=date(2026, 9, 27),
    last_day=date(2026, 10, 4),
)


def test_weekly_report_has_its_own_header_and_the_same_blocks() -> None:
    children = [
        child("Ребёнок (7 класс)", totals(4, 23, 18, 3, 2, resolved=2)),
        child("Ребёнок (3 класс)"),
    ]
    weekly = render_weekly(SUNDAY_WEEK, children)
    assert weekly == (
        "📈 Отчёт за неделю: 27 сентября – 4 октября\n"
        "\n"
        "Ребёнок (7 класс)\n"
        "Математика — 4 домашки, 23 задания:\n"
        "• верно — 18\n"
        "• с ошибкой — 3, из них разобрал с подсказками — 2\n"
        "• стоит перепроверить — 2\n"
        "\n"
        "Ребёнок (3 класс)\n"
        "Проверок за эти дни не было."
    )
    on_request = render_report(SUNDAY_WEEK, children, sends_himself=False)
    assert on_request.split("\n", 1)[1] == weekly.split("\n", 1)[1]  # блоки — одни и те же
    assert on_request.startswith("📈 Отчёт за 7 дней: ")  # заголовок по запросу — прежний


def test_week_without_homework_is_one_short_line() -> None:
    expected = (
        "📈 Отчёт за неделю: 27 сентября – 4 октября\n"
        "\n"
        "На этой неделе домашку на проверку не присылали."
    )
    assert render_weekly(SUNDAY_WEEK, [child("Ребёнок (7 класс)")]) == expected
    assert render_weekly(SUNDAY_WEEK, []) == expected


def test_long_weekly_report_is_cut_the_same_way() -> None:
    children = [busy_child(n) for n in range(1, 11)]
    text = render_weekly(SUNDAY_WEEK, children)
    assert message_length(text) <= MAX_MESSAGE_CHARS
    shown = [item for item in children if f"{item.label}\n" in text]
    assert 0 < len(shown) < 10
    assert text.endswith(f"Не показано детей: {10 - len(shown)} — отчёт не поместился в сообщение.")


def test_weekly_report_has_no_markup_ratings_or_prizes() -> None:
    children = [
        child("Ребёнок 1 (7 класс)", totals(4, 23, 18, 3, 2, resolved=2)),
        child("Ребёнок 2 (7 класс)", totals(2, 9, 9)),
    ]
    texts = [render_weekly(SUNDAY_WEEK, children), render_weekly(SUNDAY_WEEK, [])]
    texts += [weekly_switched(day, enabled=on) for day in range(7) for on in (True, False)]
    for text in texts:
        for mark in ("**", "__", "`", "<b>", "#"):
            assert mark not in text
        for word in ("рейтинг", "лучше", "хуже", "приз", "подряд", "место"):
            assert word not in text.lower()


@pytest.mark.parametrize(
    ("weekday", "days"),
    [
        (0, "по понедельникам"),
        (1, "по вторникам"),
        (2, "по средам"),
        (3, "по четвергам"),
        (4, "по пятницам"),
        (5, "по субботам"),
        (6, "по воскресеньям"),
    ],
)
def test_weekly_switch_names_the_day(weekday: int, days: str) -> None:
    assert weekly_keyboard(weekday, enabled=True) == [
        [{"type": "callback", "text": f"Не присылать {days}", "payload": "ob:weekly:off"}]
    ]
    assert weekly_keyboard(weekday, enabled=False) == [
        [{"type": "callback", "text": f"Присылать {days}", "payload": "ob:weekly:on"}]
    ]
    assert weekly_switched(weekday, enabled=True) == (
        f"Готово! Снова буду присылать отчёт о прогрессе {days}."
    )
    assert weekly_switched(weekday, enabled=False) == (
        f"Хорошо, отчёт {days} больше не присылаю. Отчёт по кнопке «📈 Отчёт о прогрессе» "
        "остаётся. Вернуть рассылку можно кнопкой ниже."
    )
