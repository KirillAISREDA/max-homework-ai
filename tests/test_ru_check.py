from hwcheck.subjects.base import Box, SubjectTask, Word
from hwcheck.subjects.russian.check import MAX_DIFF_SHARE, check_words
from hwcheck.subjects.russian.gaps import DerivedText


def _words(*texts: str, confidence: float = 0.9) -> list[Word]:
    return [
        Word(text=t, box=Box(x0=i * 40, y0=0, x1=i * 40 + 30, y1=20), confidence=confidence, line=0)
        for i, t in enumerate(texts)
    ]


def _derived(*words: str, gaps: list[int] | None = None) -> DerivedText:
    return DerivedText(
        words=list(words), gap_indices=gaps or [], trust="verified", derived_by="dictionary"
    )


def test_all_correct_gives_no_findings() -> None:
    task = SubjectTask(number="1", words=_words("Наступила", "поздняя", "осень"))
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень", gaps=[1])) == []


def test_spelling_in_gap_is_candidate_with_word_and_expected() -> None:
    task = SubjectTask(number="1", words=_words("Наступила", "позняя", "осень"))
    [finding] = check_words(3, task, _derived("Наступила", "поздняя", "осень", gaps=[1]))
    assert (finding.task_index, finding.kind, finding.strength) == (3, "spelling", "candidate")
    assert (finding.actual, finding.expected) == ("позняя", "поздняя")
    assert finding.word is not None and finding.word.box is not None and finding.line == 1
    assert finding.detail == "проверь слово «позняя»"


def test_gap_findings_come_first_then_high_confidence() -> None:
    words = _words("Настипила", "позняя", "осинь")
    words[2] = words[2].model_copy(update={"confidence": 0.3})
    task = SubjectTask(number="1", words=words)
    findings = check_words(0, task, _derived("Наступила", "поздняя", "осень", gaps=[1]))
    assert [f.actual for f in findings] == ["позняя", "Настипила", "осинь"]


def test_missing_and_extra_words() -> None:
    task = SubjectTask(number="1", words=_words("У", "гость", "был"))
    findings = check_words(0, task, _derived("У", "нас", "гость"))
    assert [(f.kind, f.actual, f.expected) for f in findings] == [
        ("missing_word", None, "нас"),
        ("extra_word", "был", None),
    ]
    missing, extra = findings
    assert missing.word is None and extra.word is not None
    assert missing.detail == "кажется, пропущено слово после «У»"


def test_too_many_differences_means_wrong_exercise() -> None:
    task = SubjectTask(number="1", words=_words("совсем", "другой", "текст", "тут"))
    [finding] = check_words(0, task, _derived("Наступила", "поздняя", "осень", "уже"))
    assert (finding.kind, finding.strength) == ("uncertain", "candidate")
    assert finding.detail == "не смог сверить с упражнением"
    assert MAX_DIFF_SHARE == 0.4


def test_unresolved_gap_is_not_a_finding() -> None:
    derived = DerivedText(
        words=["щ_ка", "плывёт"], gap_indices=[0], trust="unverified",
        derived_by="dictionary", unresolved=[0],
    )  # fmt: skip
    task = SubjectTask(number="1", words=_words("щука", "плывёт"))
    assert check_words(0, task, derived) == []  # эталона для слова нет — не спрашиваем


def test_header_line_is_skipped() -> None:
    words = [*_words("Упражнение", "245."), *_words("Наступила", "осень")]
    for w in words[2:]:
        w.line = 1
    task = SubjectTask(number="245", lines=["Упражнение 245.", "Наступила осень"], words=words)
    assert check_words(0, task, _derived("Наступила", "осень")) == []
