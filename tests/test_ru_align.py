from hwcheck.subjects.russian.align import align, display_word, normalize, similarity


def test_normalize() -> None:
    assert normalize("Ёжик,") == "ежик" and normalize("м_шина") == "мшина"


def test_normalize_drops_hyphens() -> None:
    """Дефис при сверке не значим: на краю слова это перенос или тире, которое OCR приклеил к
    соседу («дома-»), а внутри — след переноса в склеенном слове («сде-делал» — это «сделал» с
    ошибкой ребёнка, а не другое слово). «кто-то» сверяется с «кто-то» — обе стороны без дефиса."""
    assert normalize("дома-") == "дома" and normalize("-вести") == "вести"
    assert normalize("сде-делал") == "сдеделал" and normalize("кто-то") == "ктото"


def test_display_word_drops_punctuation_glued_by_ocr() -> None:
    """Вопрос ребёнку — про слово, а не про запятую рядом с ним: «здесь написано «спасти»?»."""
    assert display_word("спасти,") == "спасти"
    assert display_word("«Волгадонске».") == "Волгадонске"
    assert display_word("прово-") == "прово"
    # дефис внутри слова остаётся: «кто-то» и след переноса в склеенном «сде-делал» — то, что
    # ребёнок и написал, и именно это называет вопрос
    assert display_word("кто-то") == "кто-то" and display_word("сде-делал") == "сде-делал"


def test_display_word_of_punctuation_only_keeps_it() -> None:
    # от «!» после обрезки ничего не остаётся — показывать нечего, оставляем как прочитали
    assert display_word("!") == "!"


def test_similarity() -> None:
    assert similarity("машина", "машына") == 1 - 1 / 6
    assert similarity("кот", "собака") < 0.5


def test_align_exact_and_substitution() -> None:
    pairs = align(["Наступила", "поздняя", "осень"], ["Наступила", "позняя", "осень"])
    assert [(p.kind, p.expected_index, p.actual_index) for p in pairs] == [
        ("match", 0, 0),
        ("subst", 1, 1),
        ("match", 2, 2),
    ]


def test_align_missing_and_extra() -> None:
    pairs = align(["у", "нас", "гость"], ["у", "гость", "был"])
    assert [(p.kind, p.expected_index, p.actual_index) for p in pairs] == [
        ("match", 0, 0),
        ("missing", 1, None),
        ("match", 2, 1),
        ("extra", None, 2),
    ]


def test_align_prefers_missing_plus_extra_over_unlike_substitution() -> None:
    # «кот» и «собака» не похожи: это не описка, а пропущенное и лишнее слово
    pairs = align(["кот"], ["собака"])
    assert [p.kind for p in pairs] == ["missing", "extra"]
