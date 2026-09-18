import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from hwcheck.bench.russian import RuCaseRun, RuGoldenCase, render_ru_report, run_ru_case, score_ru
from hwcheck.subjects.base import Finding, Reference, SubjectPage, SubjectTask, TaskResult


def _case(case_id: str = "ru-01", **overrides: object) -> RuGoldenCase:
    data: dict[str, object] = {
        "schema": 1,
        "id": case_id,
        "photos": [
            {"sha256": "a" * 64, "role": "textbook"},
            {"sha256": "b" * 64, "role": "notebook"},
        ],
        "exercise": {"number": "245", "text": "Наступила п_здняя ос_нь."},
        "reference": "Наступила поздняя осень.",
        "errors": [{"written": "позняя", "expected": "поздняя", "in_gap": True}],
    }
    data.update(overrides)
    return RuGoldenCase.model_validate(data)


def _run(*findings: tuple[str, str], case_id: str = "ru-01") -> RuCaseRun:
    return RuCaseRun(
        case_id=case_id,
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


def test_score_counts_duplicate_errors_as_multiset() -> None:
    # то же слово с ошибкой два раза в кейсе — две реальные ошибки, а не одна (set схлопывал их)
    dup = _case(
        case_id="ru-02",
        errors=[
            {"written": "позняя", "expected": "поздняя", "in_gap": True},
            {"written": "позняя", "expected": "поздняя", "in_gap": True},
        ],
    )
    two_findings = score_ru(
        [dup], [_run(("spelling", "позняя"), ("spelling", "позняя"), case_id="ru-02")]
    )
    assert (two_findings.errors_total, two_findings.errors_found) == (2, 2)

    one_finding = score_ru([dup], [_run(("spelling", "позняя"), case_id="ru-02")])
    assert one_finding.errors_found == 1


# --- run_ru_case ---


def _jpeg(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@dataclass
class _FakeRuModule:
    """`SubjectModule`-подобный: recognize по фото (bytes -> страница из `pages`), resolve_reference
    и check отдают заготовленные ответы, не глядя во вход — этим тестам этого достаточно."""

    pages: dict[bytes, SubjectPage]
    references: list[Reference] = field(default_factory=list)
    results: list[TaskResult] = field(default_factory=list)
    check_error: Exception | None = None
    seen_conditions: list[SubjectTask] = field(default_factory=list)

    async def recognize(self, image: bytes) -> SubjectPage:
        return self.pages[image]

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: object | None
    ) -> list[Reference]:
        self.seen_conditions = list(tasks)
        return self.references

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]:
        if self.check_error is not None:
            raise self.check_error
        return self.results


def _photo_case(
    tmp_path: Path, textbook: bytes, notebook: bytes
) -> tuple[RuGoldenCase, dict[str, Path]]:
    textbook_path = tmp_path / "textbook.jpg"
    notebook_path = tmp_path / "notebook.jpg"
    textbook_path.write_bytes(textbook)
    notebook_path.write_bytes(notebook)
    textbook_sha = hashlib.sha256(textbook).hexdigest()
    notebook_sha = hashlib.sha256(notebook).hexdigest()
    case = _case(
        photos=[
            {"sha256": textbook_sha, "role": "textbook"},
            {"sha256": notebook_sha, "role": "notebook"},
        ]
    )
    return case, {textbook_sha: textbook_path, notebook_sha: notebook_path}


async def test_run_ru_case_happy_path(tmp_path: Path) -> None:
    textbook, notebook = _jpeg("white"), _jpeg("black")
    case, index = _photo_case(tmp_path, textbook, notebook)
    pages = {
        textbook: SubjectPage(
            subject="russian",
            role="textbook",
            tasks=[SubjectTask(number="245", condition="Наступила п_здняя ос_нь.")],
        ),
        notebook: SubjectPage(
            subject="russian", role="notebook", tasks=[SubjectTask(number="245")]
        ),
    }
    reference = Reference(task_number="245", origin="derived", trust="verified", payload={})
    finding = Finding(
        task_index=0, kind="spelling", strength="candidate", expected="поздняя", actual="позняя"
    )
    module = _FakeRuModule(
        pages=pages,
        references=[reference],
        results=[TaskResult(task_index=0, findings=[finding], reference=reference)],
    )

    run = await run_ru_case(module, None, case, index)  # type: ignore[arg-type]

    assert run.error is None
    assert run.reference_trust == "verified"
    assert [f["actual"] for f in run.findings] == ["позняя"]


def _notebook_only_case(
    tmp_path: Path, notebook: bytes, **exercise: object
) -> tuple[RuGoldenCase, dict[str, Path]]:
    path = tmp_path / "notebook.jpg"
    path.write_bytes(notebook)
    digest = hashlib.sha256(notebook).hexdigest()
    case = _case(photos=[{"sha256": digest, "role": "notebook"}], exercise=exercise)
    return case, {digest: path}


def _notebook_module(notebook: bytes) -> _FakeRuModule:
    reference = Reference(task_number="245", origin="derived", trust="verified", payload={})
    finding = Finding(
        task_index=0, kind="spelling", strength="candidate", expected="поздняя", actual="позняя"
    )
    return _FakeRuModule(
        pages={
            notebook: SubjectPage(
                subject="russian", role="notebook", tasks=[SubjectTask(number="245")]
            )
        },
        references=[reference],
        results=[TaskResult(task_index=0, findings=[finding], reference=reference)],
    )


async def test_run_ru_case_without_a_textbook_photo_uses_the_typed_exercise(
    tmp_path: Path,
) -> None:
    """Живые кейсы приходят фотографиями тетради и набранным эталоном — страницы учебника нет.
    Условие берём из разметки (`exercise.text`), иначе кейс падал с `no_reference`."""
    notebook = _jpeg("black")
    case, index = _notebook_only_case(
        tmp_path, notebook, number="245", text="Наступила п_здняя ос_нь."
    )
    module = _notebook_module(notebook)

    run = await run_ru_case(module, None, case, index)  # type: ignore[arg-type]

    assert run.error is None and [f["actual"] for f in run.findings] == ["позняя"]
    [condition] = module.seen_conditions
    assert (condition.number, condition.number_on_page) == ("245", True)
    assert condition.condition == "Наступила п_здняя ос_нь."


async def test_run_ru_case_without_a_textbook_photo_and_without_a_number(tmp_path: Path) -> None:
    notebook = _jpeg("black")
    case, index = _notebook_only_case(tmp_path, notebook, number=None, text="Наступила осень.")
    module = _notebook_module(notebook)

    await run_ru_case(module, None, case, index)  # type: ignore[arg-type]

    [condition] = module.seen_conditions
    assert (condition.number, condition.number_on_page) == ("1", False)


async def test_run_ru_case_reports_ocr_failed(tmp_path: Path) -> None:
    # OCR тетради упал: recognize() возвращает role="notebook", tasks=[], failure="ocr_failed" —
    # без исключения. Без явной проверки кейс тихо считался бы «ошибок нет» (пустой notebook).
    textbook, notebook = _jpeg("white"), _jpeg("black")
    case, index = _photo_case(tmp_path, textbook, notebook)
    pages = {
        textbook: SubjectPage(
            subject="russian",
            role="textbook",
            tasks=[SubjectTask(number="245", condition="Наступила п_здняя ос_нь.")],
        ),
        notebook: SubjectPage(subject="russian", role="notebook", tasks=[], failure="ocr_failed"),
    }
    module = _FakeRuModule(pages=pages)

    run = await run_ru_case(module, None, case, index)  # type: ignore[arg-type]

    assert run.error == "ocr_failed"


async def test_run_ru_case_reports_check_exception(tmp_path: Path) -> None:
    textbook, notebook = _jpeg("white"), _jpeg("black")
    case, index = _photo_case(tmp_path, textbook, notebook)
    pages = {
        textbook: SubjectPage(
            subject="russian",
            role="textbook",
            tasks=[SubjectTask(number="245", condition="Наступила п_здняя ос_нь.")],
        ),
        notebook: SubjectPage(
            subject="russian", role="notebook", tasks=[SubjectTask(number="245")]
        ),
    }
    reference = Reference(task_number="245", origin="derived", trust="verified", payload={})
    module = _FakeRuModule(pages=pages, references=[reference], check_error=RuntimeError("boom"))

    run = await run_ru_case(module, None, case, index)  # type: ignore[arg-type]

    assert run.error is not None and run.error.startswith("RuntimeError")
