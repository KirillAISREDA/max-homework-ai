"""Находки по выравниванию (спецификация §6): все — `candidate`, ребёнок подтверждает
«здесь написано …?». В пропуске — первыми, затем по уверенности OCR (уверенное расхождение
вероятнее ошибка ученика, неуверенное — шум распознавания)."""

from __future__ import annotations

import re

from hwcheck.subjects.base import Finding, SubjectTask, Word
from hwcheck.subjects.russian.align import Pair, align, display_word, is_word, normalize
from hwcheck.subjects.russian.gaps import DerivedText
from hwcheck.subjects.russian.recognize import merge_hyphenation

MAX_DIFF_SHARE = 0.4  # больше расхождений — это не то упражнение или не та страница
# калибруется стендом: больше половины слов — «описки» — вероятнее, плохо прочитанная страница
# целиком, а не десяток отдельных ошибок; не заваливаем ребёнка вопросами по каждому слову
MIN_WORDS_FOR_SHARE = 5
MAX_SPELLING_SHARE = 0.5
# доля слов эталона, которые нашлись на странице: ниже — упражнения на странице нет, и сверять
# нечего. Без этого порога обрезка лишнего сверху и снизу выдавала бы чужую страницу со случайным
# совпадением пары слов за верную работу (сами расхождения-то обрезаны)
MIN_MATCH_SHARE = 0.5
KIND_DETAIL = {
    "spelling": "проверь слово «{actual}»",
    "missing_word": "кажется, пропущено слово после «{previous}»",
    "missing_word_start": "кажется, в начале пропущено слово",
    "extra_word": "лишнее слово «{actual}»?",
}
UNCERTAIN_DETAIL = "не смог сверить с упражнением"
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
# «мебель» тетради: дата и заголовок работы — их нет в тексте упражнения, но ребёнок их пишет
_DATE_LINE = re.compile(rf"^\d{{1,2}}\s+(?:{_MONTHS})(?:\s+\d{{4}})?\.?$", re.IGNORECASE)
_WORK_LINE = re.compile(r"^(?:классная|домашняя)\s+работа\.?$", re.IGNORECASE)
# заголовок упражнения в начале строки: «Упр.», «Упражнение», «№», «N» — слово-маркер срезаем
# только вместе с номером, слитно («Упр.245», «№245», «N245») или следующим словом («Упражнение»,
# «245.»); без цифр это не заголовок, а слово текста («Упрямый», «Управление», «Упругий» тоже
# начинаются на «упр», но не называют упражнение — регрессия ревью 18.09)
_HEADER_KEYWORD_GLUED = re.compile(r"^(?:упр[а-яё]*|№|n)\.?\d{1,4}[.)]?$", re.IGNORECASE)
_HEADER_KEYWORD_BARE = re.compile(r"^(?:упр[а-яё]*|№|n)\.?$", re.IGNORECASE)
# номер с точкой без слова-заголовка («245. Наступила поздняя осень») — заголовок, только если
# это номер САМОГО упражнения (`task.number`) и он подтверждён страницей (`number_on_page`);
# иначе это маркер списка внутри текста («1. Яблоко...») — срезать его нельзя, «мебель» до
# первого совпадения и так поглощает `_outer_extra_indices`, если она и правда лишняя
_NUMBER_DOT = re.compile(r"^(\d{1,3})[.)]$")
# номер без точки — только сразу за словом-заголовком, иначе это число из текста («7 лет»)
_NUMBER_PLAIN = re.compile(r"^\d{1,4}[.)]?$")


def check_words(task_index: int, task: SubjectTask, derived: DerivedText) -> list[Finding]:
    expected = derived.words
    if not expected:
        return []
    # перенос «сред-/них» склеиваем до выравнивания: иначе обе половины — находки на верной копии
    words = merge_hyphenation(_body_words(task), {normalize(word) for word in expected})
    if not words:
        # эталон есть, а сверять не с чем (например, на странице — только строка-заголовок):
        # это не «всё верно», а «не разобрали текст» — так и скажем, а не промолчим
        return [_uncertain(task_index)]
    pairs = align(expected, [w.text for w in words])
    unresolved = set(derived.unresolved)
    drop = _unresolved_drop_indices(pairs, unresolved) | _outer_extra_indices(pairs)
    matched = sum(1 for p in pairs if p.kind in ("match", "subst"))
    if matched / len(expected) < MIN_MATCH_SHARE:
        return [_uncertain(task_index)]
    # доля расхождений считаем только по «пропущено/лишнее» — структурным различиям, которые и
    # означают чужой текст; описок может быть сколько угодно — это не повод считать текст не тем;
    # нерешённые пропуски (шаблон без эталона) в долю не входят — по ним и так не спросим ребёнка.
    # Считаем по окну от первого до последнего сошедшегося слова: работа сверху/снизу страницы в
    # долю не входит, иначе целая страница чужого текста рядом топит верную работу (ru-4, 18.09)
    window = _matched_window(pairs)
    structural = sum(
        1 for idx in window if pairs[idx].kind in ("missing", "extra") and idx not in drop
    )
    size = _window_size(pairs, window)
    if size and structural / size > MAX_DIFF_SHARE:
        return [_uncertain(task_index)]
    # доля описок — от числа сошедшихся пар (match + subst), а не от длины эталона: иначе
    # пропущенные ребёнком слова занижают долю и полстраницы «описок» проходят как ошибки
    aligned = [p for idx, p in enumerate(pairs) if p.kind in ("match", "subst") and idx not in drop]
    substs = sum(1 for p in aligned if p.kind == "subst")
    subst_share = substs / len(aligned) if aligned else 0.0
    if len(expected) >= MIN_WORDS_FOR_SHARE and subst_share > MAX_SPELLING_SHARE:
        return [_uncertain(task_index)]
    gaps = set(derived.gap_indices)
    scored = [
        (pair.expected_index in gaps, _finding(task_index, idx, pair, expected, words, pairs))
        for idx, pair in enumerate(pairs)
        if pair.kind != "match" and idx not in drop
    ]
    # в пропуске → первыми; орфография раньше пропущенных/лишних слов; уверенное OCR раньше шума
    scored.sort(key=lambda item: (not item[0], item[1].kind != "spelling", -_confidence(item[1])))
    return [finding for _, finding in scored]


def _uncertain(task_index: int) -> Finding:
    return Finding(
        task_index=task_index, kind="uncertain", strength="candidate", detail=UNCERTAIN_DETAIL
    )


def _unresolved_drop_indices(pairs: list[Pair], unresolved: set[int]) -> set[int]:
    """Пары нерешённого пропуска (шаблон без эталона — не с чем сверять, не спрашиваем ребёнка).
    Если align() развёл такой пропуск на «пропущено + лишнее» (шаблон не похож на слово ученика),
    обе половины — одна нерешённая пара, а не отдельные пропуск и лишнее слово."""
    drop: set[int] = set()
    for idx, pair in enumerate(pairs):
        if pair.expected_index is None or pair.expected_index not in unresolved:
            continue
        drop.add(idx)
        if pair.kind != "missing":
            continue
        if idx > 0 and pairs[idx - 1].kind == "extra":
            drop.add(idx - 1)
        if idx + 1 < len(pairs) and pairs[idx + 1].kind == "extra":
            drop.add(idx + 1)
    return drop


def _outer_extra_indices(pairs: list[Pair]) -> set[int]:
    """Лишние слова ДО первого и ПОСЛЕ последнего сошедшегося слова — не ошибки в тексте.

    Сверху это «мебель» тетради (дата, заголовок, номер), которую не разобрал `_body_words`: на
    верной странице ребёнок честно отвечает «да» на «здесь написано «17»?» — и получает ошибку
    там, где её нет (ревью 17.09). Снизу — другая работа на той же странице («Классная работа»
    со списком слов под домашней, живой прогон 18.09, ru-4).

    Цена — лишнее слово в самом конце текста: отличить его от начала другой работы нельзя,
    поэтому о нём не спрашиваем (о лишнем слове ВНУТРИ текста спрашиваем по-прежнему).

    Ни одного сошедшегося слова — не срезаем ничего: это не «мебель» вокруг текста, а другой
    текст целиком, и о нём говорят пороги `MIN_MATCH_SHARE`/`MAX_DIFF_SHARE`.
    """
    if not any(pair.kind in ("match", "subst") for pair in pairs):
        return set()
    outer: set[int] = set()
    for order in (range(len(pairs)), reversed(range(len(pairs)))):
        for idx in order:
            if pairs[idx].kind in ("match", "subst"):
                break
            if pairs[idx].kind == "extra":
                outer.add(idx)
    return outer


def _matched_window(pairs: list[Pair]) -> range:
    """Пары от первого до последнего сошедшегося слова — та часть страницы, которая и есть
    проверяемая работа. Сошедшихся слов нет — окно вся страница (о ней скажут пороги)."""
    matched = [idx for idx, pair in enumerate(pairs) if pair.kind in ("match", "subst")]
    if not matched:
        return range(len(pairs))
    return range(matched[0], matched[-1] + 1)


def _window_size(pairs: list[Pair], window: range) -> int:
    """Сколько слов эталона и тетради стоит в окне — знаменатель доли расхождений."""
    return sum(
        int(pairs[idx].expected_index is not None) + int(pairs[idx].actual_index is not None)
        for idx in window
    )


def _confidence(finding: Finding) -> float:
    # пропущенному слову не с чем сверять OCR-уверенность — это не шум распознавания, а
    # установленный факт (в тексте ученика слова нет), поэтому он не «неувереннее» описки
    if finding.word is None:
        return 1.0
    return finding.word.confidence if finding.word.confidence is not None else 0.0


def _body_words(task: SubjectTask) -> list[Word]:
    """Текст упражнения без «мебели» тетради над ним. Смотрим только на первые две строки — те
    же, что `notebook_task` отдаёт `header_number`.

    Строка целиком выбрасывается, только если она вся — дата («17 сентября») или заголовок
    работы («Домашняя работа»). Заголовок упражнения срезается словами, а не строкой: в
    «Упр. 245 Наступила поздняя осень» и «245. Наступила поздняя осень» текст стоит в той же
    строке, что номер, — выброси её целиком, и от упражнения ничего не останется (ревью 17.09).

    Слова, которые OCR не отнёс ни к одной строке (`line is None`), — пометки на полях: колонка
    цифр вдоль поля тетради, галочка учителя (живой прогон 18.09, ru-1: шесть лишних слов из
    такой колонки посреди текста). Если строк нет вообще — сверяем всё, что есть.

    Не-слова (одинокий знак «!», номер пункта «2)») выбрасываем последними: до этого они нужны —
    по ним узнаются дата и заголовок упражнения («17 сентября», «245.»).
    """
    words = [w for w in task.words if w.line is not None] or list(task.words)
    skip: set[int] = set()
    for line in sorted({w.line or 0 for w in words})[:2]:
        indices = sorted(
            (i for i, w in enumerate(words) if (w.line or 0) == line), key=lambda i: _x0(words[i])
        )
        texts = [words[i].text for i in indices]
        if _DATE_LINE.match(" ".join(texts)) or _WORK_LINE.match(" ".join(texts)):
            skip.update(indices)
            continue
        skip.update(indices[: _header_prefix(texts, task.number, task.number_on_page)])
    return [w for i, w in enumerate(words) if i not in skip and is_word(w.text)]


def _x0(word: Word) -> int:
    return word.box.x0 if word.box is not None else 0


def _header_prefix(texts: list[str], number: str, number_on_page: bool) -> int:
    """Сколько слов в начале строки — заголовок упражнения («Упр. 245», «№ 245», «245.»).

    Слово-маркер («Упр», «Упражнение», «№», «N») — заголовок только вместе с номером, слитно
    или следующим словом; без цифр это слово текста, а не заголовок (см. `_HEADER_KEYWORD_*`).
    Голый «N.» без маркера — заголовок, только если N — номер этого упражнения и он написан на
    странице (`number_on_page`); иначе это может быть маркер списка внутри текста.
    """
    target = number.strip(".)")
    index = 0
    while index < len(texts):
        text = texts[index]
        if _HEADER_KEYWORD_GLUED.match(text):
            index += 1
            continue
        has_next_number = index + 1 < len(texts) and _NUMBER_PLAIN.match(texts[index + 1])
        if _HEADER_KEYWORD_BARE.match(text) and has_next_number:
            index += 2
            continue
        dot_match = _NUMBER_DOT.match(text)
        if dot_match and number_on_page and dot_match.group(1) == target:
            index += 1
            continue
        break
    return index


def _previous_actual(pairs: list[Pair], idx: int, words: list[Word]) -> str | None:
    """Последнее слово, которое ребёнок правда написал перед пропуском (по выравниванию) — не
    слово эталона: иначе подсказка называет ребёнку то самое слово, которое он не написал."""
    for prev in reversed(pairs[:idx]):
        if prev.kind in ("match", "subst") and prev.actual_index is not None:
            return display_word(words[prev.actual_index].text)
    return None


def _finding(
    task_index: int,
    idx: int,
    pair: Pair,
    expected: list[str],
    words: list[Word],
    pairs: list[Pair],
) -> Finding:
    if pair.kind == "subst":
        assert pair.expected_index is not None and pair.actual_index is not None
        word = words[pair.actual_index]
        # в находке и в вопросе — слово без прилипших знаков препинания, в `word` — как прочитали
        actual = display_word(word.text)
        return Finding(
            task_index=task_index,
            kind="spelling",
            strength="candidate",
            expected=expected[pair.expected_index],
            actual=actual,
            word=word,
            line=(word.line or 0) + 1,
            detail=KIND_DETAIL["spelling"].format(actual=actual),
        )
    if pair.kind == "missing":
        assert pair.expected_index is not None
        previous = _previous_actual(pairs, idx, words)
        detail = (
            KIND_DETAIL["missing_word"].format(previous=previous)
            if previous is not None
            else KIND_DETAIL["missing_word_start"]
        )
        return Finding(
            task_index=task_index,
            kind="missing_word",
            strength="candidate",
            expected=expected[pair.expected_index],
            detail=detail,
        )
    assert pair.actual_index is not None
    word = words[pair.actual_index]
    actual = display_word(word.text)
    return Finding(
        task_index=task_index,
        kind="extra_word",
        strength="candidate",
        actual=actual,
        word=word,
        line=(word.line or 0) + 1,
        detail=KIND_DETAIL["extra_word"].format(actual=actual),
    )
