"""Типы контракта предметного модуля (спецификация каркаса §4): сила вердикта по находкам."""

from hwcheck.subjects.base import Finding, SubjectTask, Word, strength_of_task


def finding(strength: str, confirmed: bool | None = None) -> Finding:
    return Finding(
        task_index=0,
        kind="spelling",
        strength=strength,
        confirmed=confirmed,  # type: ignore[arg-type]
    )


def test_task_strength_is_worst_finding() -> None:
    assert strength_of_task([]) == "ok"
    assert strength_of_task([finding("feedback")]) == "feedback"
    assert strength_of_task([finding("candidate")]) == "candidate"
    assert strength_of_task([finding("candidate"), finding("verified")]) == "verified"
    # подтверждённый учеником кандидат — ошибка; отклонённый — не считается
    assert strength_of_task([finding("candidate", confirmed=True)]) == "verified"
    assert strength_of_task([finding("candidate", confirmed=False)]) == "ok"


def test_finding_is_error() -> None:
    assert finding("verified").is_error
    assert finding("candidate", confirmed=True).is_error
    assert not finding("candidate").is_error
    assert not finding("feedback").is_error


def test_subject_task_defaults() -> None:
    task = SubjectTask(number="17", condition="803 + 169", lines=["803 + 169 = 972"])
    assert task.number_on_page and task.answer is None and task.words == []
    word = Word(text="машына", confidence=0.4)
    assert word.box is None and word.line is None
