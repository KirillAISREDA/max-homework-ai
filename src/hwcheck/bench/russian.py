"""Стенд русского языка: тем же модулем, что в боте, по эталонной разметке bench/golden_ru/."""

from __future__ import annotations

import time
from collections import Counter
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
        ocr_failed = False
        for photo in case.photos:
            page = await module.recognize(index[photo.sha256].read_bytes())
            ocr_failed = ocr_failed or page.failure == "ocr_failed"
            (conditions if page.role == "textbook" else notebook).extend(page.tasks)
        # recognize() не бросает исключение ни на сбое OCR, ни на нераспознанной странице —
        # без явных проверок пустой notebook/references тихо засчитывался бы кейсом «без ошибок»
        if ocr_failed:
            return RuCaseRun(case.id, [], None, time.monotonic() - started, "ocr_failed")
        if not notebook:
            return RuCaseRun(case.id, [], None, time.monotonic() - started, "no_notebook")
        references = await module.resolve_reference(conditions, kb)
        if not references:
            return RuCaseRun(case.id, [], None, time.monotonic() - started, "no_reference")
        results = await module.check(notebook, references)
        findings = [f.model_dump() for r in results for f in r.findings]
        trust = references[0].trust
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
        # мультимножество: то же слово с ошибкой два раза в кейсе — две реальные ошибки, а не
        # одна (`set` схлопывал повторы и портил полноту — ревью)
        truth = Counter(normalize(e.written) for e in case.errors)
        candidates = [f for f in run.findings if f["kind"] != "uncertain"]
        errors_total = sum(truth.values())
        found = _count_matches(candidates, truth)
        top2 = _count_matches(candidates[:MAX_QUESTIONS], truth)
        summary.errors_total += errors_total
        summary.errors_found += found
        summary.errors_in_top2 += top2
        summary.candidates_total += len(candidates)
        summary.candidates_true += found
        summary.candidates_top2 += len(candidates[:MAX_QUESTIONS])
        summary.candidates_top2_true += top2
        summary.per_case.append(
            f"{case.id}: ошибок {errors_total}, найдено {found}, кандидатов {len(candidates)}"
        )
    return summary


def _count_matches(findings: list[dict[str, Any]], truth: Counter[str]) -> int:
    """Сколько находок совпало с реальной ошибкой — по мультимножеству: слово расходуется по
    одному разу на совпадение, не больше числа его реальных ошибок в кейсе. Совпадение находки с
    ошибкой означает одновременно и «ошибка найдена», и «кандидат — не ложный» — оба счётчика
    вызывающая сторона получает одним и тем же числом (`errors_found`/`candidates_true`)."""
    remaining = truth.copy()
    matched = 0
    for finding in findings:
        word = normalize(finding["actual"] or "")
        if remaining[word] > 0:
            remaining[word] -= 1
            matched += 1
    return matched


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
