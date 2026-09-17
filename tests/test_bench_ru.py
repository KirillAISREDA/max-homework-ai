from hwcheck.bench.russian import RuCaseRun, RuGoldenCase, render_ru_report, score_ru


def _case() -> RuGoldenCase:
    return RuGoldenCase.model_validate(
        {
            "schema": 1,
            "id": "ru-01",
            "photos": [
                {"sha256": "a" * 64, "role": "textbook"},
                {"sha256": "b" * 64, "role": "notebook"},
            ],
            "exercise": {"number": "245", "text": "Наступила п_здняя ос_нь."},
            "reference": "Наступила поздняя осень.",
            "errors": [{"written": "позняя", "expected": "поздняя", "in_gap": True}],
        }
    )


def _run(*findings: tuple[str, str]) -> RuCaseRun:
    return RuCaseRun(
        case_id="ru-01",
        findings=[{"kind": k, "actual": a, "expected": None} for k, a in findings],
        reference_trust="verified",
        seconds=1.0,
    )


def test_score_counts_found_errors_and_false_candidates() -> None:
    summary = score_ru([_case()], [_run(("spelling", "Настипила"), ("spelling", "позняя"))])
    assert (summary.errors_total, summary.errors_found, summary.errors_in_top2) == (1, 1, 1)
    assert (summary.candidates_total, summary.candidates_true) == (2, 1)
    assert summary.precision_top2 == 0.5 and summary.reference_verified == 1


def test_score_error_beyond_top2_is_missed_in_top2() -> None:
    summary = score_ru(
        [_case()], [_run(("spelling", "а"), ("spelling", "б"), ("spelling", "позняя"))]
    )
    assert (summary.errors_found, summary.errors_in_top2) == (1, 0)


def test_score_uncertain_case() -> None:
    summary = score_ru([_case()], [_run(("uncertain", ""))])
    assert summary.wrong_exercise == 1 and summary.errors_found == 0


def test_report_has_threshold_line() -> None:
    text = render_ru_report(score_ru([_case()], [_run(("spelling", "позняя"))]))
    assert "Точность кандидатов в первых двух" in text and "100%" in text
