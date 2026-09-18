from hwcheck.subjects.base import Box, SubjectTask, Word
from hwcheck.subjects.russian.check import MAX_DIFF_SHARE, MIN_MATCH_SHARE, check_words
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


def test_punctuation_glued_to_the_word_stays_out_of_the_question() -> None:
    """OCR приклеил к слову запятую («спасти,»): в вопрос и в находку идёт слово, а в `word`
    остаётся прочитанное как есть — по нему кропается картинка (живой прогон 18.09, ru-1)."""
    task = SubjectTask(number="1", words=_words("Он", "взял", "спасти,"))
    [finding] = check_words(0, task, _derived("Он", "взял", "снасти"))
    assert (finding.actual, finding.expected) == ("спасти", "снасти")
    assert finding.detail == "проверь слово «спасти»"
    assert finding.word is not None and finding.word.text == "спасти,"


def test_extra_and_missing_word_details_are_cleaned_too() -> None:
    task = SubjectTask(number="1", words=_words("Наступила", "вдруг,", "осень"))
    [extra] = check_words(0, task, _derived("Наступила", "осень"))
    assert (extra.actual, extra.detail) == ("вдруг", "лишнее слово «вдруг»?")

    task = SubjectTask(number="1", words=_words("Наступила,"))
    [missing] = check_words(0, task, _derived("Наступила", "осень"))
    assert missing.detail == "кажется, пропущено слово после «Наступила»"


def test_gap_findings_come_first_then_high_confidence() -> None:
    words = _words("Настипила", "позняя", "осинь")
    words[2] = words[2].model_copy(update={"confidence": 0.3})
    task = SubjectTask(number="1", words=words)
    findings = check_words(0, task, _derived("Наступила", "поздняя", "осень", gaps=[1]))
    assert [f.actual for f in findings] == ["позняя", "Настипила", "осинь"]


def test_missing_and_extra_words() -> None:
    # лишнее слово — внутри текста: о лишнем слове ПОСЛЕ текста не спрашиваем (см.
    # test_extra_words_after_the_text_are_not_findings)
    task = SubjectTask(number="1", words=_words("У", "был", "гость"))
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


def test_word_starting_with_upr_without_a_number_is_not_a_header() -> None:
    """«Упрямый» начинается на «упр», как и «Упр.», но без номера следом — это слово текста,
    не заголовок: `упр\\w*` без обязательных цифр срезал первое слово верной копии (ревью 18.09)."""
    words = _words("Упрямый", "осёл", "не", "пошёл", "дальше", "моста")
    task = SubjectTask(number="1", words=words)
    derived = _derived("Упрямый", "осёл", "не", "пошёл", "дальше", "моста")
    assert check_words(0, task, derived) == []


def test_word_starting_with_upr_letters_only_is_not_a_header() -> None:
    task = SubjectTask(number="1", words=_words("Управление", "заводом", "идёт", "хорошо"))
    derived = _derived("Управление", "заводом", "идёт", "хорошо")
    assert check_words(0, task, derived) == []


def test_leading_list_marker_with_other_number_is_not_a_header() -> None:
    """«1.» в начале текста — маркер списка внутри упражнения №245, а не заголовок: цифры не
    совпадают с номером задания, поэтому заголовок его не срезает — но лишнее слово перед первым
    совпадением всё равно поглощает `_leading_extra_indices`, и находки на верной копии нет."""
    task = SubjectTask(number="245", words=_lines(["1.", "Яблоко", "красное", "и", "сладкое"]))
    derived = _derived("Яблоко", "красное", "и", "сладкое")
    assert check_words(0, task, derived) == []


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


def test_another_work_below_the_text_is_not_a_finding() -> None:
    """Полуглобальная сверка (живой прогон 18.09, ru-4): на странице домашняя работа, а ниже —
    «Классная работа» со списком из тридцати слов. Раньше 90 слов страницы против 33 слов
    эталона давали долю расхождений > `MAX_DIFF_SHARE` и «не смог сверить» на верной работе."""
    other = [f"слово{index}" for index in range(30)]
    task = SubjectTask(
        number="1",
        words=_lines(
            ["17", "сентября"],
            ["Домашняя", "работа"],
            ["Наступила", "позняя", "осень"],
            ["Подул", "холодный", "ветер"],
            ["Улетели", "птицы"],
            ["Пожелтела", "трава"],
            ["Скоро", "выпадет", "снег"],
            ["Классная", "работа"],
            other[:10],
            other[10:20],
            other[20:],
        ),
    )
    derived = _derived(
        "Наступила", "поздняя", "осень", "Подул", "холодный", "ветер", "Улетели", "птицы",
        "Пожелтела", "трава", "Скоро", "выпадет", "снег",
    )  # fmt: skip
    [finding] = check_words(0, task, derived)
    assert (finding.kind, finding.actual, finding.expected) == ("spelling", "позняя", "поздняя")


def test_extra_words_after_the_text_are_not_findings() -> None:
    """Цена полуглобальной сверки: лишнее слово сразу за текстом не отличить от начала другой
    работы, поэтому о нём не спрашиваем — в отличие от лишнего слова внутри текста."""
    task = SubjectTask(number="1", words=_words("Наступила", "поздняя", "осень", "уже", "потом"))
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_page_without_the_reference_text_is_uncertain() -> None:
    """Обрезка лишнего сверху и снизу не должна выдавать чужую страницу за верную работу: с
    эталоном сошлось одно слово из шести — это не то упражнение, а случайное совпадение."""
    task = SubjectTask(
        number="1",
        words=_lines(["Классная", "работа"], ["осень", "морковь", "капуста", "свёкла"]),
    )
    derived = _derived("Наступила", "поздняя", "осень", "уже", "в", "лесу")
    [finding] = check_words(0, task, derived)
    assert (finding.kind, finding.detail) == ("uncertain", "не смог сверить с упражнением")
    assert MIN_MATCH_SHARE == 0.5


def test_margin_marks_without_a_line_are_not_findings() -> None:
    """Колонка цифр на поле тетради: OCR не отнёс их ни к одной строке — это пометки на полях,
    а не слова упражнения (живой прогон 18.09, ru-1: шесть лишних слов из такой колонки)."""
    words = _lines(["Наступила", "поздняя", "осень"])
    margin = Word(text="5", box=Box(x0=900, y0=0, x1=910, y1=20), confidence=0.4, line=None)
    task = SubjectTask(number="1", words=[words[0], margin, *words[1:]])
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


def test_hyphenated_word_split_by_line_break_is_not_a_finding() -> None:
    """Перенос «сред-/них» — одно слово эталона, а не описка плюс лишнее слово."""
    task = SubjectTask(number="1", words=_lines(["в", "сред-"], ["них", "классах"]))
    assert check_words(0, task, _derived("в", "средних", "классах")) == []


def test_childs_own_hyphenation_error_is_one_finding_with_the_left_crop() -> None:
    """Ребёнок написал «сде-делал» через перенос: это одна описка, а не половинки. В вопросе —
    слово с дефисом (так на странице), в `word` — левая половина, по ней и кроп (ревью ветки)."""
    words = _lines(["Учитель", "сде-"], ["делал", "важные", "объявления"])
    task = SubjectTask(number="1", words=words)
    [finding] = check_words(0, task, _derived("Учитель", "сделал", "важные", "объявления"))
    assert (finding.kind, finding.actual, finding.expected) == ("spelling", "сде-делал", "сделал")
    assert finding.detail == "проверь слово «сде-делал»"
    assert finding.word is not None and finding.word.box == words[1].box


def test_punctuation_and_item_numbers_of_the_page_are_not_words() -> None:
    """«!» и номер пункта «2)» словом не являются: в эталоне цифр и знаков нет (`tokenize`), с
    ними нечего сверять, а в находках это шум (живой прогон 18.09: «лишнее слово «!»»)."""
    task = SubjectTask(
        number="1", words=_words("Наступила", "!", "поздняя", "2)", "осень")
    )  # fmt: skip
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень")) == []


def test_dash_at_the_end_of_a_line_keeps_both_words() -> None:
    """«до дома — / уставшие»: обе половины есть в эталоне, а склеенного «домауставшие» нет —
    слова остаются раздельными, и остальной текст сходится (живой прогон 18.09, ru-1)."""
    task = SubjectTask(
        number="1", words=_lines(["Мы", "шли", "до", "дома-"], ["уставшие", "и", "мокрые"])
    )
    # тире на краю слова снимает `normalize`, поэтому «дома-» сходится с «дома» без находки
    derived = _derived("Мы", "шли", "до", "дома", "уставшие", "и", "мокрые")
    assert check_words(0, task, derived) == []


def test_two_consecutive_missing_words_reference_last_written_word() -> None:
    task = SubjectTask(number="1", words=_words("у", "гость"))
    findings = check_words(0, task, _derived("у", "нас", "опять", "гость"))
    assert [f.kind for f in findings] == ["missing_word", "missing_word"]
    assert all(f.detail == "кажется, пропущено слово после «у»" for f in findings)
