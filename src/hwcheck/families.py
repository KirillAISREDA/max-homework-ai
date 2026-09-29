"""Семьи, прошедшие сценарий: прислали домашку и дошли до верного ответа (веха ментора, 28.09).

Чистый подсчёт по журналу событий, как `hwcheck report cost`: сеть, база и ключи не нужны.

Что считается:
- «семья» — аккаунт, приславший домашку. За ребёнка 1–4 класса фото присылает родитель, ребёнок
  5–9 класса — сам; двое детей со своими аккаунтами — две строки;
- «дошла до верного ответа» — бот подтвердил верное решение хотя бы одного задания
  (`passed_correct`) или ребёнок довёл разбор ошибки с тьютором до верного ответа
  (`passed_fixed`). «Разобрал все ошибки домашки» считается отдельно и строже;
- аккаунты команды и тестировщиков пишутся в журнал со средой `test` и в отчёт по `prod` не
  входят (Положение, Прил. 2 п. 5.1.3).
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from hwcheck.cost import CHECK_EVENT, MSK, NO_SOURCE, START_EVENT, Row, moscow_time, select_rows

Status = Literal["passed_fixed", "passed_correct", "errors_open", "uncertain", "no_result"]

TASK_EVENT = "task_checked"
FIXED_EVENT = "error_fixed"
RESOLVED_EVENT = "homework_resolved"
FAILED_EVENT = "check_failed"
# математика пишет вердикт, языки — силу худшей находки задания (`subjects/base.py`)
CORRECT = frozenset({"correct", "ok"})
WRONG = frozenset({"wrong", "verified"})
PASSED: frozenset[Status] = frozenset({"passed_fixed", "passed_correct"})
STATUS_TEXT: dict[Status, str] = {
    "passed_fixed": "разобрал ошибку до верного ответа",
    "passed_correct": "верное решение подтверждено",
    "errors_open": "ошибки найдены, до верного ответа не дошёл",
    "uncertain": "бот не уверен, вердикта нет",
    "no_result": "задания не разобраны",
}
USER_PREFIX = 6  # знаков идентификатора в таблице: строки различимы, полный — в выгрузке JSON


class Family(BaseModel):
    user: str  # обезличенный идентификатор из журнала
    source: str  # метка первого запуска бота
    first_upload: str  # время по Москве
    last_activity: str
    active_days: int
    homeworks: int  # загрузок домашки
    clean_checks: int = 0  # проверок, где верны все задания
    failed_checks: int = 0  # проверок, упавших со сбоем
    tasks: int = 0
    correct: int = 0
    wrong: int = 0
    uncertain: int = 0
    errors_fixed: int = 0  # разборов, дошедших до верного ответа
    homeworks_resolved: int = 0  # домашек, где разобраны все ошибки
    status: Status = "no_result"


class FamiliesReport(BaseModel):
    env: str
    since: str | None = None
    data_from: str | None = None
    data_to: str | None = None
    skipped_rows: int = 0  # строки журнала без времени — в отчёт не вошли
    accounts: int = 0  # аккаунтов с любым событием за период
    uploaded: int = 0  # семей, приславших домашку
    with_result: int = 0  # из них получили вердикт хотя бы по одному заданию
    passed: int = 0  # прошли сценарий
    passed_correct: int = 0
    passed_fixed: int = 0
    resolved_all: int = 0  # разобрали все ошибки хотя бы одной домашки
    by_status: dict[str, int] = Field(default_factory=dict)  # не прошедшие — по причинам
    families: list[Family] = Field(default_factory=list)


def families_report(
    rows: Iterable[Row], *, env: str | None = "prod", since: date | None = None
) -> FamiliesReport:
    """Отчёт по среде `env` (None — все среды) с даты `since` (день по московскому времени)."""
    rows = list(rows)
    selected, skipped = select_rows(rows, env, since)
    # источник — из всей истории среды: семья могла запустить бота до начала периода
    history = selected if since is None else select_rows(rows, env, None)[0]
    sources = _first_sources(history)
    by_user: dict[str, list[Row]] = defaultdict(list)
    for row in selected:
        user = row.get("user")
        if isinstance(user, str):
            by_user[user].append(row)
    families = [
        _family(user, events, sources.get(user, NO_SOURCE))
        for user, events in by_user.items()
        if any(row.get("type") == CHECK_EVENT for row in events)
    ]
    families.sort(key=lambda family: (family.first_upload, family.user))
    statuses = Counter(family.status for family in families)
    times = [row["ts"] for row in selected]
    return FamiliesReport(
        env=env or "all",
        since=since.isoformat() if since else None,
        data_from=moscow_time(min(times)) if times else None,
        data_to=moscow_time(max(times)) if times else None,
        skipped_rows=skipped,
        accounts=len(by_user),
        uploaded=len(families),
        with_result=sum(1 for family in families if family.tasks),
        passed=sum(statuses[status] for status in PASSED),
        passed_correct=statuses["passed_correct"],
        passed_fixed=statuses["passed_fixed"],
        resolved_all=sum(1 for family in families if family.homeworks_resolved),
        by_status={
            status: count
            for status in STATUS_TEXT
            if status not in PASSED and (count := statuses[status])
        },
        families=families,
    )


def _first_sources(history: Sequence[Row]) -> dict[str, str]:
    """Метка первого запуска бота у каждого аккаунта."""
    first: dict[str, str] = {}
    for row in sorted(history, key=lambda row: row["ts"]):
        user = row.get("user")
        if row.get("type") == START_EVENT and isinstance(user, str) and user not in first:
            first[user] = str(row.get("source") or NO_SOURCE)
    return first


def _family(user: str, events: Sequence[Row], source: str) -> Family:
    uploads = [row for row in events if row.get("type") == CHECK_EVENT]
    verdicts = [str(row.get("verdict")) for row in events if row.get("type") == TASK_EVENT]
    correct = sum(1 for verdict in verdicts if verdict in CORRECT)
    wrong = sum(1 for verdict in verdicts if verdict in WRONG)
    fixed = _count(events, FIXED_EVENT)
    return Family(
        user=user,
        source=source,
        first_upload=moscow_time(min(row["ts"] for row in uploads)),
        last_activity=moscow_time(max(row["ts"] for row in events)),
        active_days=len({datetime.fromtimestamp(row["ts"], MSK).date() for row in events}),
        homeworks=len(uploads),
        clean_checks=_clean_checks(events),
        failed_checks=_count(events, FAILED_EVENT),
        tasks=len(verdicts),
        correct=correct,
        wrong=wrong,
        uncertain=len(verdicts) - correct - wrong,
        errors_fixed=fixed,
        homeworks_resolved=_count(events, RESOLVED_EVENT),
        status=_status(tasks=len(verdicts), correct=correct, wrong=wrong, fixed=fixed),
    )


def _count(events: Iterable[Row], event_type: str) -> int:
    return sum(1 for row in events if row.get("type") == event_type)


def _clean_checks(events: Iterable[Row]) -> int:
    """Проверки, где верны все задания: вердикты одной трассы с загрузкой домашки."""
    by_trace: dict[str, list[str]] = defaultdict(list)
    for row in events:
        trace = row.get("trace_id")
        if row.get("type") == TASK_EVENT and isinstance(trace, str):
            by_trace[trace].append(str(row.get("verdict")))
    return sum(1 for verdicts in by_trace.values() if all(v in CORRECT for v in verdicts))


def _status(*, tasks: int, correct: int, wrong: int, fixed: int) -> Status:
    if fixed:
        return "passed_fixed"
    if correct:
        return "passed_correct"
    if wrong:
        return "errors_open"
    return "uncertain" if tasks else "no_result"


# --- вывод текстом ---
# Консоль Windows (cp1251) не знает стрелок и знака рубля: в тексте только то, что она печатает.


def render_families_report(report: FamiliesReport) -> str:
    lines = [f"Семьи, прошедшие сценарий — среда: {report.env}"]
    if report.since:
        lines.append(f"Период: с {report.since} (по Москве)")
    if report.data_from and report.data_to:
        lines.append(f"События в журнале: {_moment(report.data_from)} — {_moment(report.data_to)}")
    if report.skipped_rows:
        lines.append(f"Строк журнала без времени (не вошли): {report.skipped_rows}")
    lines += ["", f"Аккаунтов с любым действием: {report.accounts}"]
    if not report.families:
        return "\n".join([*lines, "Прислали домашку: 0 — домашку не присылал никто."])
    lines += [
        f"Прислали домашку: {report.uploaded}",
        f"Получили вердикт хотя бы по одному заданию: {report.with_result}",
        f"Прошли сценарий: {report.passed}",
        f"  - {STATUS_TEXT['passed_fixed']}: {report.passed_fixed}",
        f"  - {STATUS_TEXT['passed_correct']}: {report.passed_correct}",
        f"Разобрали все ошибки хотя бы одной домашки: {report.resolved_all}",
    ]
    if report.by_status:
        lines.append(f"Не прошли: {report.uploaded - report.passed}")
        lines += [f"  - {STATUS_TEXT[s]}: {n}" for s, n in _statuses(report.by_status)]
    return "\n".join([*lines, "", *_table(report.families)])


def _statuses(by_status: dict[str, int]) -> list[tuple[Status, int]]:
    return [(status, by_status[status]) for status in STATUS_TEXT if status in by_status]


def _table(families: Sequence[Family]) -> list[str]:
    head = (
        "| Семья | Источник | Первая домашка | Дней | Домашек | Заданий "
        "| Верно | С ошибкой | Не уверен | Разобрано ошибок | Итог |"
    )
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for family in families:
        cells = [
            family.user[:USER_PREFIX],
            family.source,
            _moment(family.first_upload),
            str(family.active_days),
            str(family.homeworks),
            str(family.tasks),
            str(family.correct),
            str(family.wrong),
            str(family.uncertain),
            str(family.errors_fixed),
            STATUS_TEXT[family.status],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _moment(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
