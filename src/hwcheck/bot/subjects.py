"""Каталог предметов 1–9 классов и учебный год (спецификация онбординга §4.6, §5).

Источник — федеральные учебные планы ФОП НОО и ФОП ООО. Математика 7–9 классов (алгебра,
геометрия, вероятность и статистика) — одной кнопкой. `available` — проверка работает; остальные
предметы показываются как «скоро» и пишутся в лист ожидания.
"""

from dataclasses import dataclass
from datetime import date

MIN_GRADE = 1
MAX_GRADE = 9
# 1–4 класс: фото домашки присылает родитель из своего MAX — рекомендации Минпросвещения 13.09.2026
# (спецификация онбординга §4.7)
PARENT_SENDS_UP_TO_GRADE = 4


@dataclass(frozen=True)
class Subject:
    code: str
    title: str
    grades: range
    available: bool


SUBJECTS: tuple[Subject, ...] = (
    Subject("math", "Математика", range(1, 10), available=True),
    # включается после стенда `hwcheck bench ru` (≥ 10 фото, точность кандидатов ≥ 50 %) —
    # план 2026-09-17-russian-stage3 R8/R9
    Subject("russian", "Русский язык", range(1, 10), available=False),
    Subject("literary_reading", "Литературное чтение", range(1, 5), available=False),
    Subject("literature", "Литература", range(5, 10), available=False),
    Subject("foreign_language", "Иностранный язык", range(2, 10), available=False),
    Subject("world_around", "Окружающий мир", range(1, 5), available=False),
    Subject("history", "История", range(5, 10), available=False),
    Subject("social_studies", "Обществознание", range(6, 10), available=False),
    Subject("geography", "География", range(5, 10), available=False),
    Subject("biology", "Биология", range(5, 10), available=False),
    Subject("informatics", "Информатика", range(7, 10), available=False),
    Subject("physics", "Физика", range(7, 10), available=False),
    Subject("chemistry", "Химия", range(8, 10), available=False),
)
_BY_CODE = {subject.code: subject for subject in SUBJECTS}


def subjects_for(grade: int) -> list[Subject]:
    return [subject for subject in SUBJECTS if grade in subject.grades]


def subject_by_code(code: str) -> Subject | None:
    return _BY_CODE.get(code)


def school_year(day: date) -> int:
    """Учебный год по году начала: 2026/27 → 2026, новый год — с 1 сентября."""
    return day.year if day.month >= 9 else day.year - 1


def current_grade(grade: int, grade_year: int, today: date) -> int:
    """Класс сегодня: указанный класс плюс прошедшие учебные годы; больше 9 — выпускник Домашки."""
    return grade + (school_year(today) - grade_year)
