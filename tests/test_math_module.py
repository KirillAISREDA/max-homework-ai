"""Математика через контракт предметного модуля: те же вердикты, что у bot/check.py."""

from pathlib import Path

import pytest

from hwcheck.bot.check import CheckModels, validator_only_grade
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import SubjectTask
from hwcheck.subjects.math.module import (
    MathModule,
    findings_from_grade,
    to_subject_task,
    to_vision_task,
)
from hwcheck.subjects.registry import SubjectDeps, module_for

MODELS = CheckModels(vision="v", structure="s", solver="m")


def test_vision_task_round_trip() -> None:
    task = VisionTask(
        number=17,
        task_text="803 + 169",
        student_solution_steps=["803 + 169 = 972"],
        student_answer="972",
        confidence=0.9,
        number_on_page=False,
    )
    converted = to_subject_task(task)
    assert (converted.number, converted.number_on_page) == ("17", False)
    assert converted.lines == ["803 + 169 = 972"] and converted.answer == "972"
    assert to_vision_task(converted) == task
    assert to_vision_task(SubjectTask(number="задание 3")).number == 3  # цифры из номера


def test_findings_from_grade_keep_summary_texts() -> None:
    wrong = findings_from_grade(0, validator_only_grade(["2 + 2 = 5"]))
    assert [(f.kind, f.strength, f.line) for f in wrong] == [("arithmetic", "verified", 1)]
    assert findings_from_grade(0, validator_only_grade(["2 + 2 = 4"])) == []
    [unsure] = findings_from_grade(2, validator_only_grade(["<неразборчиво>"]))
    assert (unsure.strength, unsure.task_index) == ("candidate", 2)
    assert unsure.detail == "часть записи неразборчива"  # текст сводки как в handlers.py


async def test_check_without_condition_uses_validator_only(tmp_path: Path) -> None:
    module = MathModule(llm=None, models=MODELS, cache=None)  # type: ignore[arg-type]
    task = SubjectTask(number="1", lines=["999 + 1 = 1000", "950 + 50 - 660 = 320"])
    references = await module.resolve_reference([task], kb=None)
    assert references == []  # условия нет — эталона нет, LLM не вызывается
    [result] = await module.check([task], references)
    assert [(f.kind, f.line) for f in result.findings] == [("arithmetic", 2)]
    assert result.payload["grade"]["first_error_line"] == 2
    session = await module.start_tutoring(result, task, kb=None)
    assert session.ref.answer == "340" and session.first_error_line == 2


def test_registry() -> None:
    deps = SubjectDeps(llm=None, models=MODELS, cache=None)  # type: ignore[arg-type]
    assert isinstance(module_for("math", deps), MathModule)
    with pytest.raises(KeyError):
        module_for("history", deps)
