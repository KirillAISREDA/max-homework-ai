"""Стоимость проверки по журналу вызовов модели (`llm_call`) и обращения по методике конкурса.

Чистый подсчёт: на входе строки журнала событий и тарифы, на выходе отчёт. Сеть и ключи не
нужны — команда `hwcheck report cost` работает в любом контейнере с файлом журнала.

Границы подсчёта:
- в рубли входят только события `llm_call`; записи до их появления (события шагов с суммой
  токенов) не оцениваются, отчёт называет дату, с которой есть данные;
- «проверка» — трасса (один апдейт MAX) с событием `homework_uploaded`; всё остальное с
  вызовами модели — разбор с тьютором;
- модель без тарифа в рубли не входит и названа в отчёте: выдуманная цена хуже пропуска.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PRICING = Path(__file__).resolve().parents[2] / "assets" / "pricing" / "gigachat.json"
# сроки конкурса и «день пользователя» — по московскому времени; смещение постоянное, а база
# часовых поясов на Windows и в slim-образе есть не всегда
MSK = timezone(timedelta(hours=3))
CHECK_EVENT = "homework_uploaded"
LLM_EVENT = "llm_call"
# события шагов с component, которые суммируют вызовы модели: те же обращения, что и llm_call
STEP_SUMMARY_COMPONENTS = frozenset({"vision_two_stage", "solver", "classifier", "tutor"})

NO_TARIFF = "тариф не задан"

Row = dict[str, Any]


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # None — цена не сверена с официальным тарифом: вызовы модели в рубли не входят
    input_per_1m: float | None = None
    output_per_1m: float | None = None


class Pricing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: str = "RUB"
    tariff: str | None = None
    source: str | None = None
    checked_at: str | None = None
    models: dict[str, ModelPrice] = Field(default_factory=dict)

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float | None:
        """Рубли за вызов; None — у модели нет тарифа."""
        price = self.models.get(model)
        if price is None or price.input_per_1m is None or price.output_per_1m is None:
            return None
        return (tokens_in * price.input_per_1m + tokens_out * price.output_per_1m) / 1_000_000


def load_pricing(path: Path = DEFAULT_PRICING) -> Pricing:
    return Pricing.model_validate_json(path.read_text(encoding="utf-8"))


class Usage(BaseModel):
    calls: int = 0
    errors: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = 0.0  # только вызовы с тарифом; None — тарифа нет ни у одного
    unpriced_calls: int = 0


class StepUsage(Usage):
    step: str
    model: str


class Stats(BaseModel):
    mean: float | None = None
    median: float | None = None
    max: float | None = None


class CheckCost(BaseModel):
    n: int = 0  # проверки с вызовами модели — по ним среднее
    without_calls: int = 0  # загрузка была, до модели не дошло (фото не скачалось и т. п.)
    cost: Stats = Stats()
    calls: Stats = Stats()
    tokens: Stats = Stats()
    unpriced_calls: int = 0  # вызовы моделей без тарифа: суммы по проверкам без них


class TutoringCost(BaseModel):
    traces: int = 0
    users: int = 0
    calls: int = 0
    cost: float = 0.0
    per_user: float | None = None  # на пользователя, у которого был разбор
    per_check: float | None = None  # на одну проверку с вызовами модели
    unpriced_calls: int = 0


class UserCost(BaseModel):
    n: int = 0  # пользователи хотя бы с одной проверкой
    cost: Stats = Stats()
    unpriced_calls: int = 0


class ContestRequests(BaseModel):
    """Обращения по Положению (Прил. 2 п. 2.2): вызов любого компонента решения.

    Сводное событие шага и `llm_call` описывают один и тот же вызов модели. В зачёт идёт счёт
    без сводных событий: двойной счёт завышал бы метрику (Положение, п. 5.4).
    """

    component_events: int = 0
    by_component: dict[str, int] = Field(default_factory=dict)
    step_summary_events: int = 0
    component_events_without_summaries: int = 0
    user_initiated: int = 0  # внутренняя метрика: действия пользователя, считается отдельно
    checks: int = 0
    user_days: int = 0
    per_check: float | None = None
    per_user_day: float | None = None
    user_initiated_per_check: float | None = None
    user_initiated_per_user_day: float | None = None


class CostReport(BaseModel):
    env: str
    since: str | None = None
    currency: str = "RUB"
    pricing_tariff: str | None = None
    pricing_source: str | None = None
    pricing_checked_at: str | None = None
    data_from: str | None = None  # первый llm_call в журнале (МСК); None — таких событий нет
    data_to: str | None = None
    checks_before_data: int = 0  # проверки до появления llm_call — не оценены
    skipped_rows: int = 0  # строки журнала без времени (любой среды) — в отчёт не вошли
    malformed_calls: int = 0  # llm_call, у которых токены не число: посчитаны как 0 токенов
    by_step: list[StepUsage] = Field(default_factory=list)
    total: Usage = Usage()
    outside_traces: Usage = Usage()
    unpriced_models: list[str] = Field(default_factory=list)
    checks: CheckCost = CheckCost()
    tutoring: TutoringCost = TutoringCost()
    users: UserCost = UserCost()
    contest: ContestRequests = ContestRequests()


def cost_report(
    rows: Iterable[Row],
    pricing: Pricing,
    *,
    env: str | None = "prod",
    since: date | None = None,
) -> CostReport:
    """Отчёт по среде `env` (None — все среды) с даты `since` (день по московскому времени)."""
    selected, skipped = _select(rows, env, since)
    calls = [row for row in selected if row.get("type") == LLM_EVENT]
    start = _data_start(selected, calls)
    # без llm_call оценивать нечего, но обращения по событиям компонентов посчитать можно
    window = selected if start is None else [row for row in selected if row["ts"] >= start]
    before = [] if start is None else [row for row in selected if row["ts"] < start]
    priced = [_PricedCall(row, pricing) for row in calls]
    check_traces = _check_traces(window)
    checks = _check_cost(priced, check_traces) if calls else CheckCost()
    return CostReport(
        env=env or "all",
        since=since.isoformat() if since else None,
        currency=pricing.currency,
        pricing_tariff=pricing.tariff,
        pricing_source=pricing.source,
        pricing_checked_at=pricing.checked_at,
        data_from=_moscow(min(row["ts"] for row in calls)) if calls else None,
        data_to=_moscow(max(row["ts"] for row in calls)) if calls else None,
        checks_before_data=_count_checks(before if calls else selected),
        skipped_rows=skipped,
        malformed_calls=sum(1 for c in priced if c.malformed),
        by_step=_by_step(priced),
        total=_usage(priced),
        outside_traces=_usage([c for c in priced if c.trace is None]),
        unpriced_models=sorted({c.model for c in priced if c.cost is None}),
        checks=checks,
        tutoring=_tutoring_cost(priced, check_traces, checks.n),
        users=_user_cost(priced, window),
        contest=_contest(window, check_traces),
    )


class _PricedCall:
    """Событие llm_call с посчитанной ценой; поля читаются терпимо — журнал пишут разные версии."""

    def __init__(self, row: Row, pricing: Pricing) -> None:
        self.step = str(row.get("step") or "unknown")
        self.model = str(row.get("model") or "unknown")
        self.trace: str | None = row.get("trace_id")
        self.user: str | None = row.get("user")
        self.failed = row.get("status") == "error"
        self.tokens_in = _int(row.get("tokens_in"))
        self.tokens_out = _int(row.get("tokens_out"))
        # нечисловые токены считаются нулём, но вызов не должен сойти за бесплатный молча
        self.malformed = not (_is_int(row.get("tokens_in")) and _is_int(row.get("tokens_out")))
        self.cost = pricing.cost(self.model, self.tokens_in, self.tokens_out)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def _is_int(value: object) -> bool:
    """Целое число токенов; 1000.0 — тоже: так число может записать другой сериализатор."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _int(value: object) -> int:
    return int(value) if _is_int(value) and isinstance(value, int | float) else 0


def _unpriced(calls: Iterable["_PricedCall"]) -> int:
    return sum(1 for call in calls if call.cost is None)


def _select(rows: Iterable[Row], env: str | None, since: date | None) -> tuple[list[Row], int]:
    """Строки среды и периода и число строк без времени (у них не узнать ни дня, ни порядка)."""
    since_ts = (
        datetime(since.year, since.month, since.day, tzinfo=MSK).timestamp() if since else None
    )
    selected = []
    skipped = 0
    for row in rows:
        ts = row.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, int | float):
            skipped += 1
            continue
        if env is not None and row.get("env") != env:
            continue
        if since_ts is not None and ts < since_ts:
            continue
        selected.append(row)
    return selected, skipped


def _data_start(selected: Sequence[Row], calls: Sequence[Row]) -> float | None:
    """Начало окна отчёта — начало трассы первого llm_call: `homework_uploaded` той же проверки
    записано раньше вызова модели и должно попасть в окно вместе с ним."""
    if not calls:
        return None
    first = min(calls, key=lambda row: row["ts"])
    trace = first.get("trace_id")
    if trace is None:
        return float(first["ts"])
    return float(min(row["ts"] for row in selected if row.get("trace_id") == trace))


def _check_traces(rows: Iterable[Row]) -> set[str]:
    return {
        row["trace_id"]
        for row in rows
        if row.get("type") == CHECK_EVENT and row.get("trace_id") is not None
    }


def _count_checks(rows: Iterable[Row]) -> int:
    return sum(1 for row in rows if row.get("type") == CHECK_EVENT)


def _usage(calls: Iterable[_PricedCall]) -> Usage:
    usage = Usage()
    for call in calls:
        usage.calls += 1
        usage.errors += call.failed
        usage.tokens_in += call.tokens_in
        usage.tokens_out += call.tokens_out
        if call.cost is None:
            usage.unpriced_calls += 1
        else:
            usage.cost = (usage.cost or 0.0) + call.cost
    if usage.calls and usage.unpriced_calls == usage.calls:
        usage.cost = None  # тарифа нет ни у одного вызова: «0 руб.» означало бы «бесплатно»
    return usage


def _by_step(calls: Iterable[_PricedCall]) -> list[StepUsage]:
    groups: dict[tuple[str, str], list[_PricedCall]] = defaultdict(list)
    for call in calls:
        groups[call.step, call.model].append(call)
    rows = []
    for (step, model), group in groups.items():
        # тариф задан на модель: у пары «шаг, модель» цена есть у всех вызовов или ни у одного
        rows.append(StepUsage(step=step, model=model, **_usage(group).model_dump()))
    # самое дорогое — сверху; строки без тарифа — в конце
    return sorted(rows, key=lambda r: (r.cost is None, -(r.cost or 0.0), r.step, r.model))


def _stats(values: Sequence[float]) -> Stats:
    if not values:
        return Stats()
    return Stats(mean=mean(values), median=median(values), max=max(values))


def _check_cost(calls: Iterable[_PricedCall], check_traces: set[str]) -> CheckCost:
    by_trace: dict[str, list[_PricedCall]] = defaultdict(list)
    for call in calls:
        if call.trace in check_traces and call.trace is not None:
            by_trace[call.trace].append(call)
    groups = list(by_trace.values())
    return CheckCost(
        unpriced_calls=sum(_unpriced(group) for group in groups),
        n=len(groups),
        without_calls=len(check_traces) - len(groups),
        cost=_stats([sum(c.cost or 0.0 for c in group) for group in groups]),
        calls=_stats([float(len(group)) for group in groups]),
        tokens=_stats([float(sum(c.tokens for c in group)) for group in groups]),
    )


def _tutoring_cost(
    calls: Iterable[_PricedCall], check_traces: set[str], n_checks: int
) -> TutoringCost:
    tutoring = [c for c in calls if c.trace is not None and c.trace not in check_traces]
    users = {c.user for c in tutoring if c.user is not None}
    cost = sum(c.cost or 0.0 for c in tutoring)
    return TutoringCost(
        traces=len({c.trace for c in tutoring}),
        users=len(users),
        calls=len(tutoring),
        cost=cost,
        per_user=cost / len(users) if users else None,
        per_check=cost / n_checks if n_checks else None,
        unpriced_calls=_unpriced(tutoring),
    )


def _user_cost(calls: Iterable[_PricedCall], window: Iterable[Row]) -> UserCost:
    users = {
        row["user"]
        for row in window
        if row.get("type") == CHECK_EVENT and row.get("user") is not None
    }
    spent: dict[str, float] = dict.fromkeys(users, 0.0)
    unpriced = 0
    for call in calls:
        if call.user in spent and call.user is not None:
            spent[call.user] += call.cost or 0.0
            unpriced += call.cost is None
    return UserCost(n=len(users), cost=_stats(list(spent.values())), unpriced_calls=unpriced)


def _contest(window: Sequence[Row], check_traces: set[str]) -> ContestRequests:
    components = [row for row in window if row.get("component") is not None]
    actions = [row for row in window if row.get("user_initiated") is True]
    by_component = Counter(str(row["component"]) for row in components)
    summaries = sum(by_component[name] for name in STEP_SUMMARY_COMPONENTS)
    counted = [row for row in components if row["component"] not in STEP_SUMMARY_COMPONENTS]
    user_days = {
        (row["user"], datetime.fromtimestamp(row["ts"], MSK).date())
        for row in window
        if row.get("user") is not None
    }
    n_checks, n_days = len(check_traces), len(user_days)

    def in_checks(rows: Iterable[Row]) -> int:
        return sum(1 for row in rows if row.get("trace_id") in check_traces)

    def of_users(rows: Iterable[Row]) -> int:
        return sum(1 for row in rows if row.get("user") is not None)

    return ContestRequests(
        component_events=len(components),
        by_component=dict(sorted(by_component.items())),
        step_summary_events=summaries,
        component_events_without_summaries=len(components) - summaries,
        user_initiated=len(actions),
        checks=n_checks,
        user_days=n_days,
        per_check=in_checks(counted) / n_checks if n_checks else None,
        per_user_day=of_users(counted) / n_days if n_days else None,
        user_initiated_per_check=in_checks(actions) / n_checks if n_checks else None,
        user_initiated_per_user_day=of_users(actions) / n_days if n_days else None,
    )


def _moscow(ts: float) -> str:
    return datetime.fromtimestamp(ts, MSK).isoformat(timespec="seconds")


# --- вывод текстом ---
# Консоль Windows (cp1251) не знает «₽» и «→»: в тексте отчёта только то, что она печатает.


def render_cost_report(report: CostReport) -> str:
    lines = [f"Стоимость вызовов модели — среда: {report.env}", *_header(report)]
    if report.data_from is None:
        lines += ["", *_contest_lines(report.contest)]
        return "\n".join(lines)
    lines += ["", "По шагам и моделям", *_step_table(report)]
    lines += ["", *_check_lines(report), "", *_tutoring_lines(report), "", *_user_lines(report)]
    lines += ["", *_contest_lines(report.contest)]
    return "\n".join(lines)


def _header(report: CostReport) -> list[str]:
    lines = []
    if report.since:
        lines.append(f"Период: с {_day(report.since)} (день по московскому времени)")
    if report.data_from is None or report.data_to is None:
        lines.append(
            "В журнале нет событий llm_call за этот период и среду: стоимость не посчитана. "
            f"Проверок без данных о вызовах модели: {report.checks_before_data}."
        )
    else:
        lines.append(
            f"Данные llm_call: с {_moment(report.data_from)} по {_moment(report.data_to)} (МСК); "
            f"до этой даты в журнале проверок без данных о вызовах: {report.checks_before_data} "
            "— в стоимость не входят."
        )
    lines.append(
        f"Тариф: {report.pricing_tariff or 'не указан'}; источник: "
        f"{report.pricing_source or 'не указан'}; сверен: {report.pricing_checked_at or '—'}. "
        f"Суммы — {_currency(report.currency)}"
    )
    if report.unpriced_models:
        lines.append(
            f"ВНИМАНИЕ: {NO_TARIFF} для моделей: {', '.join(report.unpriced_models)} — "
            f"{report.total.unpriced_calls} вызовов в суммы не вошли, итоги занижены."
        )
    if report.malformed_calls:
        lines.append(
            f"ВНИМАНИЕ: токены не прочитаны у {report.malformed_calls} событий llm_call — "
            "посчитаны как 0 токенов, суммы занижены."
        )
    if report.skipped_rows:
        lines.append(
            f"ВНИМАНИЕ: строк журнала без времени: {report.skipped_rows} — в отчёт не вошли."
        )
    return lines


def _step_table(report: CostReport) -> list[str]:
    header = ("шаг", "модель", "вызовов", "ошибок", "токены вход", "токены выход", "сумма")
    rows = [
        (
            row.step,
            row.model,
            _num(row.calls),
            _num(row.errors),
            _num(row.tokens_in),
            _num(row.tokens_out),
            NO_TARIFF if row.cost is None else _money(row.cost),
        )
        for row in report.by_step
    ]
    total = report.total
    rows.append(
        ("ИТОГО", "", _num(total.calls), _num(total.errors), _num(total.tokens_in),
         _num(total.tokens_out), _money(total.cost))
    )  # fmt: skip
    table = [header, *rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    return [
        "  ".join(
            cell.ljust(width) if i < 2 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(line, widths, strict=True))
        ).rstrip()
        for line in table
    ]


def _check_lines(report: CostReport) -> list[str]:
    checks = report.checks
    return [
        f"Стоимость одной проверки (трассы с {CHECK_EVENT})",
        f"  проверок с вызовами модели: {checks.n}; без вызовов модели (в среднее не входят): "
        f"{checks.without_calls}",
        f"  сумма:          {_stats_line(checks.cost, _money)}{_gap(checks.unpriced_calls)}",
        f"  вызовов модели: {_stats_line(checks.calls, _decimal, _rounded)}",
        f"  токенов:        {_stats_line(checks.tokens, _rounded)}",
    ]


def _tutoring_lines(report: CostReport) -> list[str]:
    tutoring = report.tutoring
    lines = [
        f"Разбор с тьютором (трассы с вызовами модели без {CHECK_EVENT})",
        f"  трасс: {tutoring.traces}; пользователей: {tutoring.users}; вызовов модели: "
        f"{tutoring.calls}; всего: {_money(tutoring.cost)}{_gap(tutoring.unpriced_calls)}",
        f"  на пользователя с разбором: {_money(tutoring.per_user)}; "
        f"на одну проверку: {_money(tutoring.per_check)}",
    ]
    if report.outside_traces.calls:
        lines.append(
            f"  вне трасс (не проверка и не разбор): вызовов {report.outside_traces.calls}, "
            f"{_money(report.outside_traces.cost)}"
        )
    return lines


def _user_lines(report: CostReport) -> list[str]:
    return [
        "На пользователя (все его вызовы модели; пользователи хотя бы с одной проверкой)",
        f"  пользователей: {report.users.n}",
        f"  сумма:         {_stats_line(report.users.cost, _money)}"
        f"{_gap(report.users.unpriced_calls)}",
    ]


def _contest_lines(contest: ContestRequests) -> list[str]:
    by_component = ", ".join(f"{name} {count}" for name, count in contest.by_component.items())
    return [
        "Обращения по методике конкурса (события с component) — отдельно от действий пользователя",
        f"  обращений: {_num(contest.component_events_without_summaries)} — без сводных "
        f"событий шагов: они описывают те же вызовы модели, что и llm_call "
        f"({_num(contest.step_summary_events)} шт., в зачёт не идут)",
        f"  событий с component всего: {_num(contest.component_events)} ({by_component or 'нет'})",
        f"  проверок: {contest.checks}; пользователе-дней: {contest.user_days}",
        f"  обращений на проверку: {_decimal(contest.per_check)}; "
        f"на пользователя в день: {_decimal(contest.per_user_day)}",
        f"  действий пользователя (user_initiated): {_num(contest.user_initiated)}; "
        f"на проверку: {_decimal(contest.user_initiated_per_check)}; "
        f"на пользователя в день: {_decimal(contest.user_initiated_per_user_day)}",
    ]


def _stats_line(
    stats: Stats,
    fmt: Callable[[float | None], str],
    fmt_max: Callable[[float | None], str] | None = None,
) -> str:
    """`fmt_max` — когда максимум целый по природе (число вызовов), а среднее дробное."""
    largest = (fmt_max or fmt)(stats.max)
    return f"среднее {fmt(stats.mean)} · медиана {fmt(stats.median)} · максимум {largest}"


def _gap(unpriced_calls: int) -> str:
    """Пометка рядом с суммой: без неё заниженная цена выглядит как полная."""
    return f" (без {unpriced_calls} вызовов без тарифа)" if unpriced_calls else ""


def _currency(code: str) -> str:
    return "рубли, с НДС" if code == "RUB" else code


def _num(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _decimal(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}".replace(".", ",")


def _rounded(value: float | None) -> str:
    return "—" if value is None else _num(round(value))


def _moment(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")


def _day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")
