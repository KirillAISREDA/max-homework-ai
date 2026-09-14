"""Стенд сравнения моделей: эталон, метрики, кэширующий клиент, сквозной прогон."""

import hashlib
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from hwcheck.bench.client import BenchClient, BudgetExceeded
from hwcheck.bench.golden import GoldenCase, index_photos, load_cases, missing_photos
from hwcheck.bench.metrics import (
    PredictedTask,
    disagreement,
    match_tasks,
    normalize_line,
    score_lines,
    tally_verdicts,
)
from hwcheck.bench.runner import BenchConfig, render_report, run_bench, summarize
from hwcheck.llm.base import ChatMessage, LLMResult


def jpeg(color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def case_json(sha: str, **update: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": 1,
        "id": "hw4-19",
        "source": "тест",
        "photos": [{"sha256": sha, "role": "notebook"}],
        "notebook_tasks": [
            {
                "number": 19,
                "number_on_page": True,
                "lines": ["700 - (220 + 180) = 300"],
                "answer": "300",
                "verdict": "correct",
                "error_lines": [],
                "unsure": [],
            }
        ],
        "textbook_tasks": [],
        "notes": "",
    }
    data.update(update)
    return data


# --- эталон ---


def test_load_cases_and_resolve_photos(tmp_path: Path) -> None:
    photos = tmp_path / "data"
    photos.mkdir()
    image = jpeg()
    (photos / "page.jpg").write_bytes(image)
    sha = hashlib.sha256(image).hexdigest()
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "hw4-19.json").write_text(json.dumps(case_json(sha)), encoding="utf-8")

    cases = load_cases(golden)
    index = index_photos([photos])
    assert [c.id for c in cases] == ["hw4-19"]
    assert index[sha] == photos / "page.jpg"
    assert missing_photos(cases, index) == []
    assert missing_photos(cases, {}) == [("hw4-19", sha)]


@pytest.mark.parametrize(
    "broken",
    [
        {"photos": [{"sha256": "abc", "role": "notebook"}]},
        {"schema": 2},
        {
            "notebook_tasks": [
                {"number": 1, "lines": ["1 + 1 = 3"], "verdict": "wrong", "error_lines": [2]}
            ]
        },
        {"notebook_tasks": [{"number": 1, "lines": ["1 + 1 = 2"], "verdict": "maybe"}]},
    ],
)
def test_invalid_case_is_rejected_with_file_name(tmp_path: Path, broken: dict[str, Any]) -> None:
    (tmp_path / "bad.json").write_text(json.dumps(case_json("0" * 64, **broken)), encoding="utf-8")
    with pytest.raises(ValueError, match="bad.json"):
        load_cases(tmp_path)


# --- метрики ---


def test_normalize_line_ignores_spacing_and_sign_spelling() -> None:
    assert normalize_line("15 · 10 + (30 − 20) × 5 = 200") == normalize_line("15*10+(30-20)*5=200")
    assert normalize_line("4,5 + х = 7") == normalize_line("4.5+x=7")


def test_score_lines_is_order_insensitive() -> None:
    truth = ["651 + 126 = 850", "306 - 138 = 162"]
    score = score_lines(truth, ["306 - 138 = 162", "651 + 126 = 860"])
    assert score.exact == 1
    assert score.char_errors == 1
    assert score.truth_lines == 2


def test_verdict_tally_counts_false_errors_first() -> None:
    truth_ok = GoldenCase.model_validate(case_json("0" * 64)).notebook_tasks[0]
    truth_bad = truth_ok.model_copy(update={"number": 20, "verdict": "wrong", "error_lines": [1]})
    predicted = [
        PredictedTask(number=19, number_on_page=True, lines=truth_ok.lines, verdict="wrong"),
        PredictedTask(number=20, number_on_page=True, lines=truth_bad.lines, verdict="uncertain"),
    ]
    tally = tally_verdicts(match_tasks([truth_ok, truth_bad], predicted))
    assert (tally.false_error, tally.uncertain_on_wrong, tally.not_found) == (1, 1, 0)


def test_task_without_number_matched_by_lines() -> None:
    truth = GoldenCase.model_validate(case_json("0" * 64)).notebook_tasks[0]
    predicted = [
        PredictedTask(number=1, number_on_page=False, lines=truth.lines, verdict="correct")
    ]
    tally = tally_verdicts(match_tasks([truth], predicted))
    assert (tally.correct_ok, tally.not_found) == (1, 0)


def test_disagreement_between_two_transcriptions() -> None:
    truth = ["850 + 10 = 860", "15 * 10 = 150", "7 + 5 = 12"]
    run_a = ["860 + 10 = 860", "15 : 10 = 150", "7 + 5 = 12"]  # две ошибки распознавания
    run_b = ["850 + 10 = 860", "15 : 10 = 150", "7 + 5 = 12"]  # ошибка «:» у обеих
    score = disagreement(truth, run_a, run_b)
    assert (score.errors, score.flagged, score.errors_flagged) == (2, 1, 1)


# --- клиент стенда ---


class RateLimited(RuntimeError):
    status_code = 429


class CountingClient:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls = 0
        self._fail_first = fail_first

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        self.calls += 1
        if self.calls <= self._fail_first:
            raise RateLimited("Too Many Requests")
        return LLMResult(content=f"ответ {model}", model=model, tokens_in=7, tokens_out=3)

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(content="транскрипция", model=model, tokens_in=100, tokens_out=20)


async def no_sleep(_seconds: float) -> None:
    return None


async def test_bench_client_caches_by_model_and_content(tmp_path: Path) -> None:
    inner = CountingClient()
    client = BenchClient(inner, tmp_path, sleep=no_sleep)
    messages = [ChatMessage(role="user", content="реши")]
    first = await client.chat(messages, model="GigaChat-2-Pro")
    again = await client.chat(messages, model="GigaChat-2-Pro")
    other = await client.chat(messages, model="GigaChat-3-Pro")
    image = await client.analyze_image(b"img", prompt="перепиши", model="GigaChat-2-Max")
    await client.analyze_image(b"img", prompt="перепиши", model="GigaChat-2-Max")
    assert inner.calls == 3
    assert again.content == first.content and again.tokens_in == 7
    assert other.content == "ответ GigaChat-3-Pro"
    assert image.content == "транскрипция"
    assert (client.stats.fresh_calls, client.stats.cached_calls) == (3, 2)
    assert client.stats.tokens == 7 + 3 + 7 + 3 + 7 + 3 + 120 + 120


async def test_bench_client_retries_rate_limit(tmp_path: Path) -> None:
    inner = CountingClient(fail_first=2)
    client = BenchClient(inner, tmp_path, sleep=no_sleep)
    result = await client.chat([ChatMessage(role="user", content="x")], model="m")
    assert result.content == "ответ m"
    assert client.stats.rate_limited == 2


async def test_bench_client_stops_at_call_budget(tmp_path: Path) -> None:
    client = BenchClient(CountingClient(), tmp_path, max_calls=1, sleep=no_sleep)
    await client.chat([ChatMessage(role="user", content="1")], model="m")
    with pytest.raises(BudgetExceeded):
        await client.chat([ChatMessage(role="user", content="2")], model="m")
    # закэшированный вызов бюджет не тратит
    await client.chat([ChatMessage(role="user", content="1")], model="m")


# --- сквозной прогон ---

STRUCTURED = json.dumps(
    {
        "tasks": [
            {
                "number": 19,
                "task_text": "",
                "student_solution_steps": ["700 - (220 + 180) = 300"],
                "student_answer": "300",
                "confidence": 0.9,
            }
        ],
        "page_ok": True,
        "page_comment": None,
    },
    ensure_ascii=False,
)


class ScriptedGigaChat:
    """Транскрипция — всегда одна страница тетради, разбор — всегда №19."""

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        return LLMResult(content="№ 19\n700 - (220 + 180) = 300\nОтвет: 300", model=model)

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        return LLMResult(content=STRUCTURED, model=model, tokens_in=50, tokens_out=40)


async def test_run_bench_end_to_end(tmp_path: Path) -> None:
    image = jpeg()
    (tmp_path / "page.jpg").write_bytes(image)
    sha = hashlib.sha256(image).hexdigest()
    cases = [GoldenCase.model_validate(case_json(sha))]
    config = BenchConfig(
        name="baseline",
        vision_model="GigaChat-2-Max",
        structure_model="GigaChat-2-Pro",
        solver_model="GigaChat-2-Max",
    )
    client = BenchClient(ScriptedGigaChat(), tmp_path / "cache", sleep=no_sleep)
    out = tmp_path / "run.jsonl"
    runs = await run_bench(client, cases, index_photos([tmp_path]), config, out)

    summary = summarize(cases, runs)
    assert summary.verdicts.correct_ok == 1
    assert summary.verdicts.false_error == 0
    assert summary.lines.exact == 1
    assert summary.roles_correct == summary.roles_total == 1
    report = render_report([(config, summary)])
    assert "baseline" in report and "Ложные «ошибки»" in report
    assert out.read_text(encoding="utf-8").count("\n") >= 1


# --- ревью: корректность метрик ---


def test_same_number_tasks_are_matched_by_content() -> None:
    base = GoldenCase.model_validate(case_json("0" * 64)).notebook_tasks[0]
    truth_ok = base.model_copy(update={"number": 5, "lines": ["1 + 1 = 2"]})
    truth_bad = base.model_copy(
        update={"number": 5, "lines": ["2 + 2 = 5"], "verdict": "wrong", "error_lines": [1]}
    )
    predicted = [
        PredictedTask(number=5, number_on_page=True, lines=["2 + 2 = 5"], verdict="wrong"),
        PredictedTask(number=5, number_on_page=True, lines=["1 + 1 = 2"], verdict="correct"),
    ]
    tally = tally_verdicts(match_tasks([truth_ok, truth_bad], predicted))
    assert (tally.correct_ok, tally.caught, tally.false_error, tally.missed_error) == (1, 1, 0, 0)


def test_line_matching_is_optimal_not_greedy() -> None:
    score = score_lines(["abbaa", "aaaaa"], ["aaaaa", "bbbbb"])
    assert (score.exact, score.char_errors) == (1, 3)


def test_extra_predicted_lines_are_counted() -> None:
    score = score_lines(["7 + 3 = 10"], ["7 + 3 = 10", "лишняя", "ещё лишняя"])
    assert (score.exact, score.extra_lines) == (1, 2)


def test_unfinished_case_is_excluded_from_quality_metrics() -> None:
    from hwcheck.bench.runner import CaseRun

    case = GoldenCase.model_validate(case_json("0" * 64))
    two_tasks = case.model_copy(
        update={
            "notebook_tasks": [
                *case.notebook_tasks,
                case.notebook_tasks[0].model_copy(update={"number": 20}),
            ]
        }
    )
    run = CaseRun(
        case_id=case.id,
        config="c",
        roles=["notebook"],
        page_errors=[],
        transcripts=[""],
        tasks=[
            {
                "number": 19,
                "number_on_page": True,
                "lines": case.notebook_tasks[0].lines,
                "answer": "300",
                "verdict": "correct",
                "reason": None,
                "ref_status": "no_condition",
            }
        ],
        seconds=1.0,
        fresh_calls=1,
        cached_calls=0,
        tokens=10,
        rate_limited=0,
        error="BudgetExceeded: лимит",
    )
    summary = summarize([two_tasks], [run])
    assert summary.unfinished == 1
    assert (summary.verdicts.found, summary.verdicts.not_found, summary.lines.truth_lines) == (
        0,
        0,
        0,
    )


async def test_rate_limit_detected_by_status_code_only(tmp_path: Path) -> None:
    class NotRateLimited(RuntimeError):
        status_code = 500

    class Failing(CountingClient):
        async def chat(
            self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
        ) -> LLMResult:
            raise NotRateLimited("upstream error, request id 42917")

    client = BenchClient(Failing(), tmp_path, sleep=no_sleep)
    with pytest.raises(NotRateLimited):
        await client.chat([ChatMessage(role="user", content="x")], model="m")
    assert client.stats.rate_limited == 0


async def test_image_cache_key_includes_filename(tmp_path: Path) -> None:
    inner = CountingClient()
    client = BenchClient(inner, tmp_path, sleep=no_sleep)
    await client.analyze_image(b"img", prompt="p", model="m", filename="page.jpg")
    await client.analyze_image(b"img", prompt="p", model="m", filename="page.png")
    assert inner.calls == 2


def test_load_run_names_the_broken_file(tmp_path: Path) -> None:
    from hwcheck.bench.runner import load_run

    path = tmp_path / "run.jsonl"
    path.write_text(
        '{"type": "config", "name": "c", "vision_model": "a", '
        '"structure_model": "b", "solver_model": "c"}\n{"type": "ca',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="run.jsonl:2"):
        load_run(path)


def test_split_pages_collects_textbook_and_notebook() -> None:
    from hwcheck.bot.check import RecognizedPhoto, split_pages
    from hwcheck.pipeline.schemas import VisionPage, VisionTask
    from hwcheck.pipeline.vision import RecognizedPage

    def photo(role: str, task: VisionTask) -> RecognizedPhoto:
        page = VisionPage(tasks=[task], page_ok=True)
        rec = RecognizedPage(page, 0, 1, 0, 0, 0.0, "")
        return RecognizedPhoto(page=page, role=role, rec=rec)  # type: ignore[arg-type]

    condition = VisionTask(number=19, task_text="Всего 700 ребят", confidence=1)
    solution = VisionTask(
        number=19, task_text="", student_solution_steps=["700 - 400 = 300"], confidence=1
    )
    album = split_pages([photo("textbook", condition), photo("notebook", solution)], [])
    assert [t.number for t in album.textbook] == [19]
    assert album.notebook == [solution]


def test_bench_report_prints_on_legacy_windows_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прогон 14.09: печать отчёта со «→» падала в консоли cp1251 (JSONL при этом писался)."""
    import sys

    from hwcheck.cli import main

    run = tmp_path / "run.jsonl"
    run.write_text(
        '{"type": "config", "name": "c", "vision_model": "a", '
        '"structure_model": "b", "solver_model": "c"}\n',
        encoding="utf-8",
    )
    (tmp_path / "golden").mkdir()
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", console)
    main(["bench", "report", str(run), "--golden", str(tmp_path / "golden")])
    console.flush()
