"""Стоимость проверки по журналу `llm_call` и обращения по методике конкурса."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from hwcheck.cli import main
from hwcheck.config import Settings
from hwcheck.cost import (
    DEFAULT_PRICING,
    ModelPrice,
    Pricing,
    cost_report,
    load_pricing,
    render_cost_report,
)

ROOT = Path(__file__).resolve().parents[1]
MAX, PRO, LITE = "GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"
# круглые цены: суммы в тестах считаются в уме
PRICING = Pricing(
    currency="RUB",
    models={
        MAX: ModelPrice(input_per_1m=600.0, output_per_1m=600.0),
        PRO: ModelPrice(input_per_1m=500.0, output_per_1m=500.0),
        LITE: ModelPrice(input_per_1m=None, output_per_1m=None),
    },
)
DAY1 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC).timestamp()
DAY2 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC).timestamp()


def event(
    event_type: str,
    *,
    ts: float = DAY1,
    trace: str | None = "t1",
    user: str | None = "u1",
    env: str = "prod",
    component: str | None = None,
    user_initiated: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "env": env,
        "trace_id": trace,
        "type": event_type,
        "user": user,
        "component": component,
        "user_initiated": user_initiated,
        **fields,
    }


def call(
    step: str = "solver",
    model: str = MAX,
    tokens_in: int = 0,
    tokens_out: int = 0,
    *,
    status: str = "ok",
    **where: Any,
) -> dict[str, Any]:
    return event(
        "llm_call",
        component="llm",
        step=step,
        prompt_version="v1",
        model=model,
        kind="vision" if step == "vision" else "chat",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=1200,
        status=status,
        error=None if status == "ok" else "TimeoutError",
        **where,
    )


def upload(**where: Any) -> dict[str, Any]:
    return event("homework_uploaded", user_initiated=True, subject="math", **where)


def one_check(trace: str = "t1", user: str = "u1", ts: float = DAY1) -> list[dict[str, Any]]:
    """Проверка одного фото: 9 000 + 1 000 токенов Max (6 руб.) и 2 000 токенов Pro (1 руб.)."""
    where: dict[str, Any] = {"trace": trace, "user": user, "ts": ts}
    return [
        upload(**where),
        call("vision", MAX, 9_000, 1_000, **where),
        call("vision_structure", PRO, 1_500, 500, **where),
        event("vision_recognized", component="vision_two_stage", calls=2, tokens=12_000, **where),
        event("task_checked", component="validator", verdict="correct", **where),
    ]


# --- тарифы ---


def test_pricing_file_covers_every_model_of_config() -> None:
    pricing = load_pricing()
    settings = Settings(_env_file=None)
    models = {
        settings.vision_model, settings.solver_model, settings.tutor_model, settings.lite_model
    }  # fmt: skip
    assert models <= set(pricing.models)
    assert pricing.currency == "RUB"
    assert pricing.source is not None and pricing.source.startswith("https://developers.sber.ru/")
    assert pricing.checked_at is not None and date.fromisoformat(pricing.checked_at)
    assert pricing.tariff  # какой именно тариф взят — записано в файле
    for price in pricing.models.values():
        for value in (price.input_per_1m, price.output_per_1m):
            assert value is None or value > 0


def test_pricing_file_gets_into_the_image() -> None:
    """`.dockerignore` исключает assets целиком — без исключения файла тарифов в образе нет."""
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!assets/pricing" in lines
    assert DEFAULT_PRICING == ROOT / "assets" / "pricing" / "gigachat.json"


def test_price_is_per_million_tokens_by_direction() -> None:
    pricing = Pricing(
        currency="RUB", models={"m": ModelPrice(input_per_1m=100.0, output_per_1m=300.0)}
    )
    assert pricing.cost("m", 1_000_000, 0) == pytest.approx(100.0)
    assert pricing.cost("m", 500_000, 1_000_000) == pytest.approx(350.0)


def test_price_is_unknown_for_missing_or_empty_tariff() -> None:
    assert PRICING.cost("GigaChat-9", 1_000, 1_000) is None
    assert PRICING.cost(LITE, 1_000, 1_000) is None


# --- разбивка по шагам и моделям ---


def test_calls_are_grouped_by_step_and_model() -> None:
    rows = [
        *one_check(),
        call("solver", MAX, 800, 200),
        call("solver", MAX, 0, 0, status="error"),
    ]
    report = cost_report(rows, PRICING)
    by_step = {(r.step, r.model): r for r in report.by_step}
    assert set(by_step) == {("vision", MAX), ("vision_structure", PRO), ("solver", MAX)}
    solver = by_step["solver", MAX]
    assert (solver.calls, solver.errors) == (2, 1)
    assert (solver.tokens_in, solver.tokens_out) == (800, 200)
    assert solver.cost == pytest.approx(0.6)
    assert by_step["vision", MAX].cost == pytest.approx(6.0)
    assert by_step["vision_structure", PRO].cost == pytest.approx(1.0)
    assert (report.total.calls, report.total.errors) == (4, 1)
    assert report.total.cost == pytest.approx(7.6)


def test_model_without_tariff_is_reported_not_priced() -> None:
    rows = [*one_check(), call("tutor", LITE, 5_000, 500, trace="t2")]
    report = cost_report(rows, PRICING)
    lite = next(r for r in report.by_step if r.model == LITE)
    assert lite.cost is None and lite.unpriced_calls == 1
    assert report.unpriced_models == [LITE]
    assert report.total.cost == pytest.approx(7.0)  # без вызовов, у которых нет тарифа
    assert report.total.unpriced_calls == 1
    text = render_cost_report(report)
    assert "тариф не задан" in text and LITE in text


def test_total_is_unknown_when_no_call_has_a_tariff() -> None:
    """Ноль рублей — это утверждение «бесплатно»; когда тарифа нет ни у одного вызова, суммы нет."""
    report = cost_report([upload(), call("tutor", LITE, 5_000, 500)], PRICING)
    assert report.total.cost is None and report.total.unpriced_calls == 1
    assert report.by_step[0].cost is None
    assert report.outside_traces.cost == 0.0  # вызовов вне трасс не было: ноль настоящий


def test_whole_float_token_count_is_read_as_number() -> None:
    row = {**call("solver", MAX, 0, 0), "tokens_in": 1_000.0, "tokens_out": 1_000.0}
    report = cost_report([upload(), row], PRICING)
    assert (report.total.tokens_in, report.total.tokens_out) == (1_000, 1_000)
    assert report.malformed_calls == 0
    assert report.total.cost == pytest.approx(1.2)


# --- стоимость одной проверки ---


def test_check_cost_is_mean_median_max_over_traces_with_upload() -> None:
    rows = [
        *one_check("t1"),  # 7 руб.
        *one_check("t2"),
        call("solver", MAX, 4_000, 1_000, trace="t2"),  # 7 + 3 = 10 руб.
        *one_check("t3", user="u2"),
        call("solver", MAX, 20_000, 5_000, trace="t3", user="u2"),  # 7 + 15 = 22 руб.
    ]
    checks = cost_report(rows, PRICING).checks
    assert checks.n == 3
    assert checks.cost.mean == pytest.approx(13.0)
    assert checks.cost.median == pytest.approx(10.0)
    assert checks.cost.max == pytest.approx(22.0)
    assert checks.calls.mean == pytest.approx(8 / 3)
    assert checks.tokens.max == 37_000


def test_check_without_model_calls_is_counted_apart() -> None:
    """Фото не скачалось — вызовов модели нет: нулевая «проверка» занизила бы среднюю цену."""
    rows = [*one_check("t1"), upload(trace="t2"), event("check_failed", trace="t2")]
    checks = cost_report(rows, PRICING).checks
    assert (checks.n, checks.without_calls) == (1, 1)
    assert checks.cost.mean == pytest.approx(7.0)


# --- разбор с тьютором и стоимость на пользователя ---


def test_tutoring_is_calls_of_traces_without_upload() -> None:
    rows = [
        *one_check("t1", user="u1"),
        *one_check("t2", user="u2"),
        event("button_pressed", trace="t3", user="u1", user_initiated=True),
        call("classifier", PRO, 1_000, 1_000, trace="t3", user="u1"),  # 1 руб.
        call("tutor", PRO, 3_000, 1_000, trace="t3", user="u1"),  # 2 руб.
        event("message_received", trace="t4", user="u1", user_initiated=True),
        call("tutor", PRO, 5_000, 1_000, trace="t4", user="u1"),  # 3 руб.
    ]
    tutoring = cost_report(rows, PRICING).tutoring
    assert (tutoring.traces, tutoring.users, tutoring.calls) == (2, 1, 3)
    assert tutoring.cost == pytest.approx(6.0)
    assert tutoring.per_user == pytest.approx(6.0)  # на пользователя, который разбирал ошибки
    assert tutoring.per_check == pytest.approx(3.0)  # на каждую из двух проверок


def test_cost_per_user_is_all_his_calls_over_users_with_checks() -> None:
    rows = [
        *one_check("t1", user="u1"),  # 7
        call("tutor", PRO, 5_000, 1_000, trace="t2", user="u1"),  # 3
        *one_check("t3", user="u2"),  # 7
        call("tutor", PRO, 1_000, 1_000, trace="t4", user="u3"),  # без проверок — не в счёт
    ]
    users = cost_report(rows, PRICING).users
    assert users.n == 2
    assert users.cost.mean == pytest.approx(8.5)
    assert users.cost.max == pytest.approx(10.0)


# --- среда, период, записи до выкатки ---


def test_only_prod_by_default() -> None:
    rows = [
        *one_check("t1"),
        *[{**row, "env": "test"} for row in one_check("t2", user="tester")],
        *[{**row, "env": "dev"} for row in one_check("t3", user="dev")],
    ]
    assert cost_report(rows, PRICING).checks.n == 1
    assert cost_report(rows, PRICING, env="test").checks.n == 1
    assert cost_report(rows, PRICING, env=None).checks.n == 3


def test_since_cuts_earlier_days_by_moscow_time() -> None:
    # 30.09 00:30 по Москве — это ещё 29.09 по UTC: день считается по московскому времени
    night = datetime(2026, 9, 29, 21, 30, tzinfo=UTC).timestamp()
    rows = [*one_check("t1", ts=DAY1), *one_check("t2", ts=night), *one_check("t3", ts=DAY2)]
    report = cost_report(rows, PRICING, since=date(2026, 9, 30))
    assert report.checks.n == 2
    assert report.since == "2026-09-30"


def test_rows_before_llm_call_rollout_are_not_priced() -> None:
    old = datetime(2026, 9, 20, 12, 0, tzinfo=UTC).timestamp()
    rows = [
        upload(trace="old", ts=old),
        event(
            "vision_recognized", component="vision_two_stage", tokens=12_000, trace="old", ts=old
        ),
        upload(trace="t1", ts=DAY1),  # событие загрузки раньше первого llm_call той же трассы
        call("vision", MAX, 9_000, 1_000, trace="t1", ts=DAY1 + 5),
    ]
    report = cost_report(rows, PRICING)
    assert report.checks.n == 1 and report.checks.without_calls == 0
    assert report.checks_before_data == 1
    assert report.data_from is not None and report.data_from.startswith("2026-09-29")
    assert report.contest.component_events == 1  # старые события в окно отчёта не входят
    text = render_cost_report(report)
    assert "29.09.2026" in text and "до этой даты" in text


def test_journal_without_llm_calls_says_so() -> None:
    rows = [upload(), event("vision_recognized", component="vision_two_stage", tokens=12_000)]
    report = cost_report(rows, PRICING)
    assert report.data_from is None and report.total.calls == 0 and report.checks.n == 0
    assert report.checks_before_data == 1
    assert "нет событий llm_call" in render_cost_report(report)


def test_broken_rows_are_skipped_and_counted() -> None:
    """Пропуск строк виден читателю: иначе сбой записи журнала выглядит как тихий день."""
    rows = [*one_check(), {"type": "llm_call"}, {"ts": "вчера", "env": "prod", "type": "llm_call"}]
    report = cost_report(rows, PRICING)
    assert report.total.calls == 2
    assert report.skipped_rows == 2
    assert "без времени" in render_cost_report(report)
    assert "без времени" not in render_cost_report(cost_report(one_check(), PRICING))


def test_call_with_broken_token_count_is_counted_not_hidden_as_free() -> None:
    broken = {**call("solver", MAX, 0, 200), "tokens_in": "много"}
    report = cost_report([*one_check(), broken], PRICING)
    assert report.malformed_calls == 1
    assert report.total.calls == 3  # вызов состоялся, но его токены входа неизвестны
    assert "токены не прочитаны" in render_cost_report(report)


def test_unpriced_calls_are_visible_in_every_total() -> None:
    """Модель без тарифа занижает и цену проверки, и цену разбора — это видно рядом с суммой."""
    rows = [
        *one_check("t1", user="u1"),
        call("solver", LITE, 5_000, 500, trace="t1", user="u1"),
        call("tutor", LITE, 5_000, 500, trace="t2", user="u1"),
        call("tutor", PRO, 1_000, 1_000, trace="t2", user="u1"),
    ]
    report = cost_report(rows, PRICING)
    assert (report.checks.unpriced_calls, report.checks.cost.mean) == (1, pytest.approx(7.0))
    assert (report.tutoring.unpriced_calls, report.tutoring.cost) == (1, pytest.approx(1.0))
    assert report.users.unpriced_calls == 2
    text = render_cost_report(report)
    assert text.count("без тарифа") >= 3


# --- обращения по методике конкурса ---


def test_contest_requests_are_component_events_apart_from_user_actions() -> None:
    rows = [
        *one_check("t1", user="u1", ts=DAY1),  # 4 события компонентов, 1 действие
        event("button_pressed", trace="t2", user="u1", ts=DAY1, user_initiated=True),
        call("tutor", PRO, 1_000, 100, trace="t2", user="u1", ts=DAY1),
        event("tutor_reply", component="tutor", trace="t2", user="u1", ts=DAY1),
        *one_check("t3", user="u1", ts=DAY2),
        *one_check("t4", user="u2", ts=DAY2),
        event("notify_sent", component="notifier", trace="t4", user="u3", ts=DAY2),
    ]
    contest = cost_report(rows, PRICING).contest
    assert contest.component_events == 15
    assert contest.by_component == {
        "llm": 7, "notifier": 1, "tutor": 1, "validator": 3, "vision_two_stage": 3
    }  # fmt: skip
    assert contest.user_initiated == 4
    assert contest.checks == 3
    assert contest.per_check == pytest.approx(13 / 3)  # события трасс с загрузкой домашки
    assert contest.user_days == 4  # u1 два дня, u2 и u3 по одному
    assert contest.per_user_day == pytest.approx(15 / 4)
    assert contest.user_initiated_per_check == pytest.approx(1.0)
    assert contest.user_initiated_per_user_day == pytest.approx(1.0)
    # сводные события шагов описывают те же вызовы модели, что и llm_call
    assert contest.step_summary_events == 4
    assert contest.component_events_without_summaries == 11


# --- вывод ---


def full_journal() -> list[dict[str, Any]]:
    return [
        *one_check("t1", user="u1"),
        call("solver", MAX, 800, 200, trace="t1"),
        event("button_pressed", trace="t2", user="u1", user_initiated=True),
        call("tutor", PRO, 3_000, 1_000, trace="t2", user="u1"),
        call("tutor", LITE, 3_000, 1_000, trace="t2", user="u1", status="error"),
    ]


def test_report_text_is_russian_table_printable_on_windows_console() -> None:
    text = render_cost_report(cost_report(full_journal(), PRICING))
    for expected in (
        "Стоимость одной проверки",
        "Разбор с тьютором",
        "На пользователя",
        "Обращения по методике конкурса",
        "vision_structure",
        MAX,
        "среда: prod",
    ):
        assert expected in text
    assert "7,60" in text  # рубли — с запятой, как принято в отчётах проекта
    text.encode("cp1251")  # консоль Windows: ни «₽», ни «→» в выводе быть не должно


def write_journal(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


def write_pricing(path: Path) -> Path:
    path.write_text(PRICING.model_dump_json(), encoding="utf-8")
    return path


def test_cli_report_cost_prints_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    journal = write_journal(tmp_path / "events.jsonl", full_journal())
    pricing = write_pricing(tmp_path / "pricing.json")
    main(["report", "cost", str(journal), "--pricing", str(pricing)])
    out = capsys.readouterr().out
    assert "Стоимость одной проверки" in out and "7,60" in out


def test_cli_report_cost_json_and_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [*full_journal(), *[{**r, "env": "test"} for r in one_check("t9", user="tester")]]
    journal = write_journal(tmp_path / "events.jsonl", rows)
    pricing = write_pricing(tmp_path / "pricing.json")
    common = ["report", "cost", str(journal), "--pricing", str(pricing), "--json"]

    main(common)
    prod = json.loads(capsys.readouterr().out)
    assert prod["env"] == "prod" and prod["checks"]["n"] == 1
    assert prod["total"]["cost"] == pytest.approx(9.6)

    main([*common, "--env", "test"])
    assert json.loads(capsys.readouterr().out)["total"]["cost"] == pytest.approx(7.0)

    main([*common, "--env", "all"])
    assert json.loads(capsys.readouterr().out)["checks"]["n"] == 2

    main([*common, "--since", "2026-10-01"])
    assert json.loads(capsys.readouterr().out)["total"]["calls"] == 0


def test_cli_report_cost_uses_repository_pricing_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = write_journal(tmp_path / "events.jsonl", full_journal())
    main(["report", "cost", str(journal), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["pricing_source"].startswith("https://developers.sber.ru/")


def test_cli_report_without_cost_is_the_old_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runbook зовёт `hwcheck report var/events.jsonl` — сводка вердиктов остаётся на месте."""
    rows = [event("task_checked", component="validator", verdict="correct")]
    journal = write_journal(tmp_path / "events.jsonl", rows)
    main(["report", str(journal)])
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "prod": {"verdicts": {"correct": 1}, "uncertain_reasons": {}, "clarified": {}}
    }


def test_cli_report_rejects_bad_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["report", "cost", "a.jsonl", "b.jsonl"])
    with pytest.raises(SystemExit):
        main(["report", "cost", "--since", "вчера"])
    with pytest.raises(SystemExit):
        main(["report", "cost", "--pricing", str(tmp_path / "нет-такого.json")])


def test_cli_report_cost_says_when_journal_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Опечатка в пути не должна выглядеть как «вызовов модели не было»."""
    with pytest.raises(SystemExit):
        main(["report", "cost", str(tmp_path / "evnets.jsonl")])
    assert "evnets.jsonl" in capsys.readouterr().err


def test_cli_report_cost_says_when_pricing_is_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = write_journal(tmp_path / "events.jsonl", full_journal())
    pricing = tmp_path / "pricing.json"
    for broken in ("{не json", '{"models": {"GigaChat-2": {"input_per_1m": "дорого"}}}'):
        pricing.write_text(broken, encoding="utf-8")
        with pytest.raises(SystemExit):
            main(["report", "cost", str(journal), "--pricing", str(pricing)])
        assert "pricing.json" in capsys.readouterr().err
