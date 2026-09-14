"""Прогон конфигурации моделей по эталонным кейсам тем же кодом, что в боте, и отчёт.

Прогон пишет JSONL (`.cache/bench/runs/`, в git не попадает: там транскрипции детских тетрадей);
отчёт (`bench/reports/`) — только числа.
"""

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hwcheck.bench.client import BenchClient, BudgetExceeded
from hwcheck.bench.golden import GoldenCase
from hwcheck.bench.metrics import (
    DisagreementScore,
    LineScore,
    PredictedTask,
    VerdictTally,
    disagreement,
    match_tasks,
    score_lines,
    tally_verdicts,
)
from hwcheck.bot.check import CheckModels, RecognizedPhoto, check_task, recognize_photo, split_pages
from hwcheck.bot.pages import attach_conditions


class BenchConfig(BaseModel):
    name: str
    vision_model: str
    structure_model: str
    solver_model: str

    @property
    def models(self) -> CheckModels:
        return CheckModels(
            vision=self.vision_model, structure=self.structure_model, solver=self.solver_model
        )


@dataclass
class CaseRun:
    case_id: str
    config: str
    roles: list[str | None]  # роль каждой страницы; None — фото не распознано
    page_errors: list[str]
    transcripts: list[str]
    tasks: list[dict[str, Any]]
    seconds: float
    fresh_calls: int
    cached_calls: int
    tokens: int
    rate_limited: int
    error: str | None = None


async def run_case(
    client: BenchClient, case: GoldenCase, index: dict[str, Path], config: BenchConfig
) -> CaseRun:
    before = client.stats.snapshot()
    started = time.monotonic()
    recognized: list[RecognizedPhoto] = []
    roles: list[str | None] = []
    transcripts: list[str] = []
    page_errors: list[str] = []
    tasks: list[dict[str, Any]] = []
    error: str | None = None
    try:
        for photo in case.photos:
            try:
                result = await recognize_photo(
                    client, index[photo.sha256].read_bytes(), config.models
                )
            except BudgetExceeded:
                raise
            except Exception as exc:  # сбой одной страницы — метрика, а не падение прогона
                page_errors.append(type(exc).__name__)
                roles.append(None)
                transcripts.append("")
                continue
            recognized.append(result)
            roles.append(result.role)
            transcripts.append(result.rec.raw)
        album = split_pages(recognized, [])
        for task in attach_conditions(album.notebook, album.textbook):
            checked = await check_task(client, task, config.models, cache=None)
            tasks.append(
                {
                    "number": task.number,
                    "number_on_page": task.number_on_page,
                    "lines": task.student_solution_steps,
                    "answer": task.student_answer,
                    "verdict": checked.grade.verdict,
                    "reason": checked.grade.uncertain_reason,
                    "ref_status": checked.ref_status,
                }
            )
    except BudgetExceeded as exc:
        error = f"BudgetExceeded: {exc}"
    after = client.stats
    return CaseRun(
        case_id=case.id,
        config=config.name,
        roles=roles,
        page_errors=page_errors,
        transcripts=transcripts,
        tasks=tasks,
        seconds=round(time.monotonic() - started, 2),
        fresh_calls=after.fresh_calls - before.fresh_calls,
        cached_calls=after.cached_calls - before.cached_calls,
        tokens=after.tokens - before.tokens,
        rate_limited=after.rate_limited - before.rate_limited,
        error=error,
    )


async def run_bench(
    client: BenchClient,
    cases: list[GoldenCase],
    index: dict[str, Path],
    config: BenchConfig,
    out: Path,
) -> list[CaseRun]:
    out.parent.mkdir(parents=True, exist_ok=True)
    runs = []
    with out.open("w", encoding="utf-8") as sink:
        sink.write(json.dumps({"type": "config", **config.model_dump()}, ensure_ascii=False) + "\n")
        for case in cases:
            print(f"[{config.name}] {case.id} ...", flush=True)
            run = await run_case(client, case, index, config)
            sink.write(json.dumps({"type": "case", **asdict(run)}, ensure_ascii=False) + "\n")
            sink.flush()
            runs.append(run)
            if run.error and run.error.startswith("BudgetExceeded"):
                break
    return runs


def load_run(path: Path) -> tuple[BenchConfig, list[CaseRun]]:
    config: BenchConfig | None = None
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        kind = row.pop("type")
        if kind == "config":
            config = BenchConfig.model_validate(row)
        elif kind == "case":
            runs.append(CaseRun(**row))
    if config is None:
        raise ValueError(f"{path}: нет строки config")
    return config, runs


@dataclass
class BenchSummary:
    cases: int = 0
    lines: LineScore = field(default_factory=LineScore)
    verdicts: VerdictTally = field(default_factory=VerdictTally)
    roles_correct: int = 0
    roles_total: int = 0
    uncertain_reasons: Counter[str] = field(default_factory=Counter)
    ref_status: Counter[str] = field(default_factory=Counter)
    page_errors: int = 0
    unfinished: int = 0
    seconds: float = 0.0
    fresh_calls: int = 0
    cached_calls: int = 0
    tokens: int = 0
    rate_limited: int = 0


def _predicted(run: CaseRun) -> list[PredictedTask]:
    return [
        PredictedTask(
            number=t["number"],
            number_on_page=t["number_on_page"],
            lines=t["lines"],
            verdict=t["verdict"],
        )
        for t in run.tasks
    ]


def summarize(cases: list[GoldenCase], runs: list[CaseRun]) -> BenchSummary:
    by_id = {c.id: c for c in cases}
    summary = BenchSummary()
    for run in runs:
        case = by_id.get(run.case_id)
        if case is None:
            continue
        summary.cases += 1
        truth_lines = [line for task in case.notebook_tasks for line in task.lines]
        predicted_lines = [line for task in run.tasks for line in task["lines"]]
        summary.lines.add(score_lines(truth_lines, predicted_lines))
        summary.verdicts.add(tally_verdicts(match_tasks(case.notebook_tasks, _predicted(run))))
        summary.roles_total += len(case.photos)
        summary.roles_correct += sum(
            1 for photo, role in zip(case.photos, run.roles, strict=False) if photo.role == role
        )
        for task in run.tasks:
            summary.ref_status[task["ref_status"]] += 1
            if task["verdict"] == "uncertain":
                summary.uncertain_reasons[task["reason"] or "unknown"] += 1
        summary.page_errors += len(run.page_errors)
        summary.unfinished += int(run.error is not None)
        summary.seconds += run.seconds
        summary.fresh_calls += run.fresh_calls
        summary.cached_calls += run.cached_calls
        summary.tokens += run.tokens
        summary.rate_limited += run.rate_limited
    return summary


def pair_disagreement(
    cases: list[GoldenCase], runs_a: list[CaseRun], runs_b: list[CaseRun]
) -> DisagreementScore:
    by_case_b = {r.case_id: r for r in runs_b}
    score = DisagreementScore()
    for case in cases:
        run_a = next((r for r in runs_a if r.case_id == case.id), None)
        run_b = by_case_b.get(case.id)
        if run_a is None or run_b is None:
            continue
        truth = [line for task in case.notebook_tasks for line in task.lines]
        lines_a = [line for task in run_a.tasks for line in task["lines"]]
        lines_b = [line for task in run_b.tasks for line in task["lines"]]
        score.add(disagreement(truth, lines_a, lines_b))
    return score


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "—"


def render_report(
    results: list[tuple[BenchConfig, BenchSummary]],
    pairs: list[tuple[str, str, DisagreementScore]] | None = None,
) -> str:
    header = "| Метрика | " + " | ".join(config.name for config, _ in results) + " |"
    rule = "|---|" + "---|" * len(results)
    rows: list[tuple[str, list[str]]] = [
        (
            "Модели (распознавание / разбор / эталон)",
            [f"{c.vision_model} / {c.structure_model} / {c.solver_model}" for c, _ in results],
        ),
        ("Кейсов", [str(s.cases) for _, s in results]),
        (
            "Ложные «ошибки» (верное → ошибка)",
            [_pct(s.verdicts.false_error, s.verdicts.truth_correct) for _, s in results],
        ),
        (
            "Пропущенные ошибки (ошибка → верно)",
            [_pct(s.verdicts.missed_error, s.verdicts.truth_wrong) for _, s in results],
        ),
        (
            "Верные вердикты",
            [_pct(s.verdicts.correct_ok + s.verdicts.caught, s.verdicts.found) for _, s in results],
        ),
        (
            "«Не уверен»",
            [
                _pct(
                    s.verdicts.uncertain_on_correct + s.verdicts.uncertain_on_wrong,
                    s.verdicts.found,
                )
                for _, s in results
            ],
        ),
        (
            "Задание не найдено",
            [
                _pct(s.verdicts.not_found, s.verdicts.found + s.verdicts.not_found)
                for _, s in results
            ],
        ),
        (
            "Строки точно как в тетради",
            [_pct(s.lines.exact, s.lines.truth_lines) for _, s in results],
        ),
        ("Символьные ошибки строк (CER)", [f"{100 * s.lines.cer:.1f}%" for _, s in results]),
        ("Роль страницы верна", [_pct(s.roles_correct, s.roles_total) for _, s in results]),
        (
            "Токены всего / на кейс",
            [f"{s.tokens} / {s.tokens // s.cases if s.cases else 0}" for _, s in results],
        ),
        ("Вызовы свежие / из кэша", [f"{s.fresh_calls} / {s.cached_calls}" for _, s in results]),
        (
            "429 / сбои страниц / не дошли до конца",
            [f"{s.rate_limited} / {s.page_errors} / {s.unfinished}" for _, s in results],
        ),
        ("Время, с", [f"{s.seconds:.0f}" for _, s in results]),
        (
            "Причины «не уверен»",
            [
                ", ".join(f"{k} {v}" for k, v in s.uncertain_reasons.most_common()) or "—"
                for _, s in results
            ],
        ),
        (
            "Эталон (ref_status)",
            [
                ", ".join(f"{k} {v}" for k, v in s.ref_status.most_common()) or "—"
                for _, s in results
            ],
        ),
    ]
    lines = [header, rule, *(f"| {name} | " + " | ".join(values) + " |" for name, values in rows)]
    if pairs:
        lines += [
            "",
            "**Две расшифровки** — ошибки распознавания A, попавшие в места расхождения A и B:",
            "",
            "| A / B | Ошибок в строках A | Расхождений | Ошибки в расхождениях (полнота) "
            "| Точность | Расхождений на кейс, макс |",
            "|---|---|---|---|---|---|",
        ]
        for a, b, score in pairs:
            lines.append(
                f"| {a} / {b} | {_pct(score.errors, score.lines)} | {score.flagged} | "
                f"{_pct(score.errors_flagged, score.errors)} | {100 * score.precision:.0f}% | "
                f"{max(score.per_case_flagged, default=0)} |"
            )
    return "\n".join(lines) + "\n"
