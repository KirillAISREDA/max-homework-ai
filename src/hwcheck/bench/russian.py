"""Стенд русского языка: тем же модулем, что в боте, по эталонной разметке bench/golden_ru/."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hwcheck.bench.golden import GoldenPhoto
from hwcheck.bot.clarify import MAX_QUESTIONS
from hwcheck.subjects.base import KnowledgeBase, SubjectModule, SubjectTask
from hwcheck.subjects.russian.align import normalize


class RuExerciseGold(BaseModel):
    number: str | None = None
    text: str


class RuError(BaseModel):
    written: str
    expected: str
    in_gap: bool = True


class RuGoldenCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schema")
    id: str
    source: str = ""
    photos: list[GoldenPhoto] = Field(min_length=1, max_length=4)
    exercise: RuExerciseGold
    reference: str
    errors: list[RuError] = Field(default_factory=list)
    notes: str = ""


def load_ru_cases(directory: Path) -> list[RuGoldenCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(RuGoldenCase.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    return cases


@dataclass
class RuCaseRun:
    case_id: str
    findings: list[dict[str, Any]]
    reference_trust: str | None
    seconds: float
    error: str | None = None


async def run_ru_case(
    module: SubjectModule, kb: KnowledgeBase | None, case: RuGoldenCase, index: dict[str, Path]
) -> RuCaseRun:
    started = time.monotonic()
    try:
        conditions: list[SubjectTask] = []
        notebook: list[SubjectTask] = []
        for photo in case.photos:
            page = await module.recognize(index[photo.sha256].read_bytes())
            (conditions if page.role == "textbook" else notebook).extend(page.tasks)
        references = await module.resolve_reference(conditions, kb)
        results = await module.check(notebook, references)
        findings = [f.model_dump() for r in results for f in r.findings]
        trust = references[0].trust if references else None
    except Exception as exc:  # сбой кейса — строка отчёта, не падение прогона
        return RuCaseRun(
            case.id, [], None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
        )
    return RuCaseRun(case.id, findings, trust, time.monotonic() - started)


@dataclass
class RuSummary:
    cases: int = 0
    errors_total: int = 0
    errors_found: int = 0
    errors_in_top2: int = 0
    candidates_total: int = 0
    candidates_true: int = 0
    candidates_top2: int = 0
    candidates_top2_true: int = 0
    reference_verified: int = 0
    wrong_exercise: int = 0
    failed: int = 0
    per_case: list[str] = field(default_factory=list)

    @property
    def precision_top2(self) -> float:
        return self.candidates_top2_true / self.candidates_top2 if self.candidates_top2 else 0.0


def score_ru(cases: list[RuGoldenCase], runs: list[RuCaseRun]) -> RuSummary:
    by_id = {r.case_id: r for r in runs}
    summary = RuSummary()
    for case in cases:
        run = by_id.get(case.id)
        if run is None or run.error:
            summary.failed += 1
            continue
        summary.cases += 1
        summary.reference_verified += int(run.reference_trust == "verified")
        if any(f["kind"] == "uncertain" for f in run.findings):
            summary.wrong_exercise += 1
        truth = {normalize(e.written) for e in case.errors}
        candidates = [f for f in run.findings if f["kind"] != "uncertain"]
        summary.errors_total += len(truth)
        found = {normalize(f["actual"] or "") for f in candidates} & truth
        top2 = {normalize(f["actual"] or "") for f in candidates[:MAX_QUESTIONS]} & truth
        summary.errors_found += len(found)
        summary.errors_in_top2 += len(top2)
        summary.candidates_total += len(candidates)
        summary.candidates_true += sum(normalize(f["actual"] or "") in truth for f in candidates)
        summary.candidates_top2 += len(candidates[:MAX_QUESTIONS])
        summary.candidates_top2_true += len(top2)
        summary.per_case.append(
            f"{case.id}: ошибок {len(truth)}, найдено {len(found)}, кандидатов {len(candidates)}"
        )
    return summary


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "—"


def render_ru_report(summary: RuSummary) -> str:
    rows = [
        ("Кейсов (+ упавших)", f"{summary.cases} (+{summary.failed})"),
        ("Реальные ошибки найдены", _pct(summary.errors_found, summary.errors_total)),
        (
            "Реальные ошибки в первых двух вопросах",
            _pct(summary.errors_in_top2, summary.errors_total),
        ),
        ("Точность кандидатов (все)", _pct(summary.candidates_true, summary.candidates_total)),
        (
            "Точность кандидатов в первых двух",
            _pct(summary.candidates_top2_true, summary.candidates_top2),
        ),
        ("Эталон verified", _pct(summary.reference_verified, summary.cases)),
        ("«Не то упражнение» (uncertain)", str(summary.wrong_exercise)),
    ]
    table = "| Метрика | Значение |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
    return table + "\n\n" + "\n".join(f"- {line}" for line in summary.per_case) + "\n"
