"""Примеры из соседних колонок в одной строке (стенд 14.09, альбом hw2 №20).

Распознавание пишет колонки через широкий пробел, разбор 2-Pro их не делит вопреки промпту:
«180 - x = 100      x - 17 = 40» читалось как одно уравнение → ложная «ошибка».
"""

from typing import Any

import pytest

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.pages import split_columns

HW2_TASK_20 = [
    "180 - x = 100      x - 17 = 40",
    "x = 180 - 100       x = 17 + 40",
    "x = 80              x = 57",
    "180 - 80 = 100      57 - 17 = 40",
    "100 = 100           40 = 40",
    "x + 24 = 50",
    "x = 50 - 24",
    "x = 26",
]


def test_column_block_is_read_column_by_column() -> None:
    assert split_columns(HW2_TASK_20) == [
        "180 - x = 100",
        "x = 180 - 100",
        "x = 80",
        "180 - 80 = 100",
        "100 = 100",
        "x - 17 = 40",
        "x = 17 + 40",
        "x = 57",
        "57 - 17 = 40",
        "40 = 40",
        "x + 24 = 50",
        "x = 50 - 24",
        "x = 26",
    ]


def test_split_task_is_no_longer_a_false_error() -> None:
    assert validator_only_grade(HW2_TASK_20).verdict == "wrong"  # как было на стенде
    assert validator_only_grade(split_columns(HW2_TASK_20)).verdict == "correct"


def test_gap_without_equality_on_both_sides_is_kept() -> None:
    lines = ["803 + 169      425 + 375", "Всего - 180 стр.     1 д. - 52 стр.", "x = 7    Ответ"]
    assert split_columns(lines) == lines


def test_ordinary_lines_are_unchanged() -> None:
    lines = ["15 * 10 + (30 - 20) * 5 = 200", "  = 320 : 4", "5 · 171 = 855"]
    assert split_columns(lines) == lines


def test_separate_blocks_keep_their_order() -> None:
    lines = [
        "1 + 1 = 2    2 + 2 = 4",
        "Проверка:",
        "3 + 3 = 6    4 + 4 = 8",
        "5 + 5 = 10    6 + 6 = 12",
    ]
    assert split_columns(lines) == [
        "1 + 1 = 2",
        "2 + 2 = 4",
        "Проверка:",
        "3 + 3 = 6",
        "5 + 5 = 10",
        "4 + 4 = 8",
        "6 + 6 = 12",
    ]


def test_tab_separated_columns_are_split() -> None:
    assert split_columns(["1 + 1 = 2\t2 + 2 = 4"]) == ["1 + 1 = 2", "2 + 2 = 4"]


async def test_recognize_photo_splits_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    from hwcheck.bot import check
    from hwcheck.pipeline.schemas import VisionPage, VisionTask
    from hwcheck.pipeline.vision import RecognizedPage

    page = VisionPage(
        tasks=[
            VisionTask(number=20, task_text="", student_solution_steps=HW2_TASK_20, confidence=1)
        ],
        page_ok=True,
    )

    async def fake_recognize(*_args: Any, **_kw: Any) -> RecognizedPage:
        return RecognizedPage(page, 0, 1, 0, 0, 0.0, "№ 20")

    monkeypatch.setattr(check, "recognize_page_two_stage", fake_recognize)
    models = check.CheckModels(vision="v", structure="s", solver="m")
    recognized = await check.recognize_photo(None, b"img", models)  # type: ignore[arg-type]
    assert recognized.page is not None
    assert recognized.page.tasks[0].student_solution_steps == split_columns(HW2_TASK_20)
