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


def test_unresolved_gap_split_into_missing_and_extra_produces_no_finding() -> None:
    # шаблон «м_» не похож на «мяч» (буквенное сходство < 0.5) — align разводит пропуск на
    # «пропущено + лишнее»; без эталона для пропуска не спрашиваем ни о том, ни о другом
    derived = DerivedText(
        words=["м_", "лежит", "на", "полу"], gap_indices=[0], trust="unverified",
        derived_by="dictionary", unresolved=[0],
    )  # fmt: skip
    task = SubjectTask(number="1", words=_words("мяч", "лежит", "на", "полу"))
    assert check_words(0, task, derived) == []


def test_only_header_words_with_reference_is_uncertain() -> None:
    task = SubjectTask(number="245", words=_words("Упражнение", "245."))
    [finding] = check_words(0, task, _derived("Наступила", "осень"))
    assert (finding.kind, finding.strength, finding.detail) == (
        "uncertain",
        "candidate",
        "не смог сверить с упражнением",
    )


def _lines(*lines: list[str]) -> list[Word]:
    """Слова тетради по строкам: номер строки — её порядок, x0 — порядок слова в строке."""
    words = []
    for number, texts in enumerate(lines):
        for position, text in enumerate(texts):
            words.append(
                Word(
                    text=text,
                    box=Box(
                        x0=position * 40, y0=number * 30, x1=position * 40 + 30, y1=number * 30 + 20
                    ),
                    confidence=0.9,
                    line=number,
                )  # fmt: skip
            )
    return words


def test_date_line_before_header_is_not_a_finding() -> None:
    """Дата над номером упражнения — «мебель» тетради, а не лишние слова: иначе честное «да»
    ребёнка на «здесь написано «17»?» превращается в ошибку на верной странице (ревью 17.09)."""
    task = SubjectTask(
        number="245",
        words=_lines(["17", "сентября"], ["Упражнение", "245."], ["Наступила", "осень"]),
    )
    assert check_words(0, task, _derived("Наступила", "осень")) == []


def test_number_on_the_text_line_is_stripped_not_the_line() -> None:
    task = SubjectTask(
        number="245", number_on_page=False,
        words=_lines(["245.", "Наступила", "поздняя", "осень"]),
    )  # fmt: skip
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_header_words_before_text_in_one_line_are_stripped() -> None:
    task = SubjectTask(number="245", words=_lines(["Упр.", "245", "Наступила", "поздняя", "осень"]))
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_work_heading_line_is_not_a_finding() -> None:
    task = SubjectTask(
        number="1", number_on_page=False,
        words=_lines(["Домашняя", "работа"], ["Наступила", "поздняя", "осень"]),
    )  # fmt: skip
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_unrecognised_furniture_before_the_text_is_dropped() -> None:
    """Запасной путь для того, что не разобрали как заголовок («17.09.2026»): лишние слова до
    первого совпадения — то, что ребёнок написал над упражнением, а не ошибка в тексте."""
    task = SubjectTask(
        number="1", number_on_page=False,
        words=_lines(["17.09.2026"], ["Наступила", "поздняя", "осень"]),
    )  # fmt: skip
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_too_many_near_miss_words_is_uncertain() -> None:
    task = SubjectTask(
        number="1",
        words=_words("Настипила", "позняя", "осинь", "приходет", "каждой", "код"),
    )
    derived = _derived("Наступила", "поздняя", "осень", "приходит", "каждый", "год")
    [finding] = check_words(0, task, derived)
    assert (finding.kind, finding.detail) == ("uncertain", "не смог сверить с упражнением")


def test_few_near_miss_words_gives_spelling_findings() -> None:
    task = SubjectTask(
        number="1",
        words=_words("Настипила", "поздняя", "осинь", "приходит", "каждой", "год"),
    )
    derived = _derived("Наступила", "поздняя", "осень", "приходит", "каждый", "год")
    findings = check_words(0, task, derived)
    assert [f.kind for f in findings] == ["spelling", "spelling", "spelling"]
    assert {f.actual for f in findings} == {"Настипила", "осинь", "каждой"}


def test_missing_word_at_start_has_generic_detail() -> None:
    task = SubjectTask(number="1", words=_words("были"))
    [finding] = check_words(0, task, _derived("Жили", "были"))
    assert finding.detail == "кажется, в начале пропущено слово"


def test_two_consecutive_missing_words_reference_last_written_word() -> None:
    task = SubjectTask(number="1", words=_words("у", "гость"))
    findings = check_words(0, task, _derived("у", "нас", "опять", "гость"))
    assert [f.kind for f in findings] == ["missing_word", "missing_word"]
    assert all(f.detail == "кажется, пропущено слово после «у»" for f in findings)
