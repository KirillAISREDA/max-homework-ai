"""Каталог предметов 1–9 классов и учебный год (спецификация онбординга §4.6, §5)."""

from datetime import date

import pytest

from hwcheck.bot.subjects import (
    MAX_GRADE,
    MIN_GRADE,
    SUBJECTS,
    current_grade,
    school_year,
    subject_by_code,
    subjects_for,
)


def codes(grade: int) -> list[str]:
    return [s.code for s in subjects_for(grade)]


def test_primary_school_subjects() -> None:
    assert codes(1) == ["math", "russian", "literary_reading", "world_around"]
    assert codes(2) == ["math", "russian", "literary_reading", "foreign_language", "world_around"]


def test_middle_school_subjects() -> None:
    assert codes(5) == [
        "math", "russian", "literature", "foreign_language", "history", "geography", "biology",
    ]  # fmt: skip
    assert "social_studies" in codes(6) and "social_studies" not in codes(5)
    assert "physics" in codes(7) and "informatics" in codes(7) and "chemistry" not in codes(7)
    assert len(codes(9)) == 11 and "chemistry" in codes(9)


def test_only_math_is_available_and_grades_stay_in_range() -> None:
    assert [s.code for s in SUBJECTS if s.available] == ["math"]
    assert all(MIN_GRADE <= g <= MAX_GRADE for s in SUBJECTS for g in s.grades)
    assert subject_by_code("history") is not None
    assert subject_by_code("english") is None


@pytest.mark.parametrize(
    ("day", "year"),
    [(date(2026, 8, 31), 2025), (date(2026, 9, 1), 2026), (date(2027, 1, 15), 2026)],
)
def test_school_year_starts_on_september_first(day: date, year: int) -> None:
    assert school_year(day) == year


def test_current_grade_moves_with_school_year() -> None:
    assert current_grade(4, 2025, date(2026, 8, 31)) == 4
    assert current_grade(4, 2025, date(2026, 9, 1)) == 5
    assert current_grade(9, 2026, date(2027, 9, 1)) == 10  # выпускник Домашки
