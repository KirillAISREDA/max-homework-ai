"""Отчёт «семьи, прошедшие сценарий»: прислали домашку и дошли до верного ответа."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from hwcheck.cli import main
from hwcheck.families import families_report, render_families_report

DAY1 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp()
DAY2 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC).timestamp()


def event(
    event_type: str,
    *,
    ts: float = DAY1,
    trace: str | None = "t1",
    user: str | None = "u1",
    env: str = "prod",
    **fields: Any,
) -> dict[str, Any]:
    return {"ts": ts, "env": env, "trace_id": trace, "type": event_type, "user": user, **fields}


def check(user: str, trace: str, verdicts: list[str], *, ts: float = DAY1) -> list[dict[str, Any]]:
    """Загрузка домашки и вердикты её заданий — одна трасса."""
    rows = [event("homework_uploaded", user=user, trace=trace, ts=ts, subject="math")]
    rows += [
        event("task_checked", user=user, trace=trace, ts=ts + 1, verdict=verdict)
        for verdict in verdicts
    ]
    return rows


def family(report: Any, user: str) -> Any:
    return next(f for f in report.families if f.user == user)


def test_family_with_all_tasks_correct_passed_the_scenario() -> None:
    report = families_report(check("u1", "t1", ["correct", "correct"]))
    assert (report.uploaded, report.with_result, report.passed) == (1, 1, 1)
    assert (report.passed_correct, report.passed_fixed) == (1, 0)
    one = family(report, "u1")
    assert one.status == "passed_correct"
    assert (one.homeworks, one.tasks, one.correct, one.clean_checks) == (1, 2, 2, 1)


def test_family_that_fixed_an_error_with_tutor_passed() -> None:
    rows = [
        *check("u1", "t1", ["wrong", "uncertain"]),
        event("error_fixed", user="u1", trace="t2", ts=DAY1 + 60),
    ]
    report = families_report(rows)
    assert (report.passed, report.passed_fixed, report.passed_correct) == (1, 1, 0)
    assert report.resolved_all == 0  # разобрана одна ошибка, событие «все разобраны» не пришло
    assert family(report, "u1").status == "passed_fixed"


def test_resolving_all_errors_of_a_homework_is_counted_separately() -> None:
    rows = [
        *check("u1", "t1", ["wrong"]),
        event("error_fixed", user="u1", trace="t2", ts=DAY1 + 60),
        event("homework_resolved", user="u1", trace="t2", ts=DAY1 + 61, component="notifier"),
    ]
    report = families_report(rows)
    assert (report.passed, report.resolved_all) == (1, 1)
    assert family(report, "u1").homeworks_resolved == 1


def test_families_that_did_not_reach_a_correct_answer() -> None:
    rows = [
        *check("errors", "t1", ["wrong", "wrong"]),
        *check("unsure", "t2", ["uncertain"]),
        *check("nothing", "t3", []),
        event("check_failed", user="nothing", trace="t3", ts=DAY1 + 2, error="GatewayError"),
    ]
    report = families_report(rows)
    assert (report.uploaded, report.with_result, report.passed) == (3, 2, 0)
    assert family(report, "errors").status == "errors_open"
    assert family(report, "unsure").status == "uncertain"
    nothing = family(report, "nothing")
    assert (nothing.status, nothing.failed_checks) == ("no_result", 1)
    assert report.by_status == {"errors_open": 1, "uncertain": 1, "no_result": 1}


def test_partly_correct_homework_counts_as_a_confirmed_correct_answer() -> None:
    """Бот подтвердил хотя бы одно верное решение — семья дошла до верного ответа; «всё верно
    сразу» при этом не засчитано."""
    report = families_report(check("u1", "t1", ["correct", "uncertain"]))
    one = family(report, "u1")
    assert (one.status, one.clean_checks) == ("passed_correct", 0)


def test_language_subjects_use_strength_of_findings_as_verdict() -> None:
    rows = [
        *check("ok", "t1", ["ok"]),
        *check("found", "t2", ["verified", "candidate", "feedback"]),
    ]
    report = families_report(rows)
    assert family(report, "ok").status == "passed_correct"
    found = family(report, "found")
    assert (found.correct, found.wrong, found.uncertain) == (0, 1, 2)
    assert found.status == "errors_open"


def test_report_counts_accounts_that_started_but_sent_nothing() -> None:
    rows = [
        event("bot_started", user="silent", trace="t0", source="kanal-1"),
        *check("u1", "t1", ["correct"]),
    ]
    report = families_report(rows)
    assert (report.accounts, report.uploaded) == (2, 1)
    assert [f.user for f in report.families] == ["u1"]  # в списке — только приславшие домашку


def test_report_respects_environment_and_period() -> None:
    rows = [
        *check("tester", "t1", ["correct"]),
        *check("old", "t2", ["correct"], ts=DAY1),
        *check("new", "t3", ["correct"], ts=DAY2),
    ]
    for row in rows[:2]:
        row["env"] = "test"
    assert [f.user for f in families_report(rows).families] == ["old", "new"]
    assert [f.user for f in families_report(rows, env="test").families] == ["tester"]
    assert len(families_report(rows, env=None).families) == 3
    late = families_report(rows, since=date(2026, 10, 2))
    assert [f.user for f in late.families] == ["new"]
    assert late.since == "2026-10-02"


def test_traffic_source_comes_from_the_first_start_even_before_the_period() -> None:
    rows = [
        event("bot_started", user="u1", trace="t0", ts=DAY1, source="kanal-1"),
        event("bot_started", user="u1", trace="t9", ts=DAY1 + 5, source="chat-2"),
        *check("u1", "t1", ["correct"], ts=DAY2),
        *check("u2", "t2", ["correct"], ts=DAY2),
    ]
    report = families_report(rows, since=date(2026, 10, 2))
    assert family(report, "u1").source == "kanal-1"
    assert family(report, "u2").source == "не записан"


def test_rows_without_time_or_user_do_not_break_the_report() -> None:
    rows = [
        {"type": "homework_uploaded", "env": "prod", "user": "u9"},  # без времени
        event("homework_uploaded", user=None, trace="t5"),  # без пользователя
        event("task_checked", user="u1", trace=None, verdict="correct"),  # без трассы
        *check("u1", "t1", ["correct"]),
    ]
    report = families_report(rows)
    assert report.skipped_rows == 1
    assert [f.user for f in report.families] == ["u1"]
    assert family(report, "u1").tasks == 2


def test_families_are_listed_by_first_upload_with_moscow_time() -> None:
    rows = [
        *check("later", "t2", ["correct"], ts=DAY2),
        *check("first", "t1", ["wrong"], ts=DAY1),
        *check("first", "t3", ["correct"], ts=DAY2 + 100),
    ]
    report = families_report(rows)
    assert [f.user for f in report.families] == ["first", "later"]
    first = family(report, "first")
    assert first.first_upload == "2026-10-01T15:00:00+03:00"
    assert first.last_activity == "2026-10-02T15:01:41+03:00"
    assert (first.homeworks, first.active_days) == (2, 2)


def test_empty_journal_gives_an_empty_report() -> None:
    report = families_report([])
    assert (report.accounts, report.uploaded, report.passed, report.families) == (0, 0, 0, [])
    assert "домашку не присылал никто" in render_families_report(report)


def test_text_report_names_the_numbers_and_prints_on_a_russian_windows_console() -> None:
    rows = [
        event("bot_started", user="aaaaaa111111", trace="t0", source="kanal-1"),
        *check("aaaaaa111111", "t1", ["wrong"]),
        event("error_fixed", user="aaaaaa111111", trace="t2", ts=DAY1 + 60),
        *check("bbbbbb222222", "t3", ["uncertain"]),
    ]
    text = render_families_report(families_report(rows))
    assert "Прислали домашку: 2" in text
    assert "Прошли сценарий: 1" in text
    assert "aaaaaa" in text and "aaaaaa111111" not in text  # в таблице — начало идентификатора
    assert "kanal-1" in text
    text.encode("cp1251")  # консоль Windows: без стрелок и знака рубля


def test_cli_report_families(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    journal = tmp_path / "events.jsonl"
    rows = check("u1", "t1", ["correct"])
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    main(["report", "families", str(journal)])
    assert "Прошли сценарий: 1" in capsys.readouterr().out

    main(["report", "families", str(journal), "--json", "--env", "all"])
    data = json.loads(capsys.readouterr().out)
    assert (data["passed"], data["families"][0]["user"]) == (1, "u1")

    main(["report", "families", str(journal), "--since", "2026-11-01"])
    assert "домашку не присылал никто" in capsys.readouterr().out


def test_cli_report_families_refuses_missing_journal_and_bad_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["report", "families", str(tmp_path / "нет.jsonl")])
    assert "журнал событий не найден" in capsys.readouterr().err
    journal = tmp_path / "events.jsonl"
    journal.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["report", "families", str(journal), "--since", "вчера"])
    assert "--since" in capsys.readouterr().err
