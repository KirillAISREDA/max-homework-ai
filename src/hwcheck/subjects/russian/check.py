"""Находки по выравниванию (спецификация §6): все — `candidate`, ребёнок подтверждает
«здесь написано …?». В пропуске — первыми, затем по уверенности OCR (уверенное расхождение
вероятнее ошибка ученика, неуверенное — шум распознавания)."""

from __future__ import annotations

import re

from hwcheck.subjects.base import Finding, SubjectTask, Word
from hwcheck.subjects.russian.align import Pair, align
from hwcheck.subjects.russian.gaps import DerivedText

MAX_DIFF_SHARE = 0.4  # больше расхождений — это не то упражнение или не та страница
# калибруется стендом: больше половины слов — «описки» — вероятнее, плохо прочитанная страница
# целиком, а не десяток отдельных ошибок; не заваливаем ребёнка вопросами по каждому слову
MIN_WORDS_FOR_SHARE = 5
MAX_SPELLING_SHARE = 0.5
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
# заголовок упражнения в начале строки: «Упр.», «Упражнение», «№», «N» (номер бывает слитно)
_HEADER_KEYWORD = re.compile(r"^(?:упр\w*|№|n)\.?\s*(?:\d{1,4}[.)]?)?$", re.IGNORECASE)
# номер с точкой — заголовок и без слова «упражнение» («245. Наступила поздняя осень»);
# номер без точки — только сразу за словом-заголовком, иначе это число из текста («7 лет»)
_NUMBER_DOT = re.compile(r"^\d{1,3}[.)]$")
_NUMBER_PLAIN = re.compile(r"^\d{1,4}[.)]?$")


def check_words(task_index: int, task: SubjectTask, derived: DerivedText) -> list[Finding]:
    expected = derived.words
    if not expected:
        return []
    words = _body_words(task)
    if not words:
        # эталон есть, а сверять не с чем (например, на странице — только строка-заголовок):
        # это не «всё верно», а «не разобрали текст» — так и скажем, а не промолчим
        return [_uncertain(task_index)]
    pairs = align(expected, [w.text for w in words])
    unresolved = set(derived.unresolved)
    drop = _unresolved_drop_indices(pairs, unresolved) | _leading_extra_indices(pairs)
    # доля расхождений считаем только по «пропущено/лишнее» — структурным различиям, которые и
    # означают чужой текст; описок может быть сколько угодно — это не повод считать текст не тем;
    # нерешённые пропуски (шаблон без эталона) в долю не входят — по ним и так не спросим ребёнка
    structural = sum(
        1 for idx, p in enumerate(pairs) if p.kind in ("missing", "extra") and idx not in drop
    )
    if structural / (len(expected) + len(words)) > MAX_DIFF_SHARE:
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


def _leading_extra_indices(pairs: list[Pair]) -> set[int]:
    """Лишние слова ДО первого сошедшегося слова — «мебель» тетради (дата, заголовок, номер),
    которую не разобрал `_body_words`, а не ошибка в тексте: на верной странице ребёнок честно
    отвечает «да» на «здесь написано «17»?» — и получает ошибку там, где её нет (ревью 17.09).

    Ни одного сошедшегося слова — не срезаем ничего: это не «мебель» перед текстом, а другой
    текст целиком, и о нём говорят пороги `MAX_DIFF_SHARE`/`MAX_SPELLING_SHARE`.
    """
    if not any(pair.kind in ("match", "subst") for pair in pairs):
        return set()
    leading: set[int] = set()
    for idx, pair in enumerate(pairs):
        if pair.kind in ("match", "subst"):
            break
        if pair.kind == "extra":
            leading.add(idx)
    return leading


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
    """
    words = list(task.words)
    skip: set[int] = set()
    for line in sorted({w.line or 0 for w in words})[:2]:
        indices = sorted(
            (i for i, w in enumerate(words) if (w.line or 0) == line), key=lambda i: _x0(words[i])
        )
        texts = [words[i].text for i in indices]
        if _DATE_LINE.match(" ".join(texts)) or _WORK_LINE.match(" ".join(texts)):
            skip.update(indices)
            continue
        skip.update(indices[: _header_prefix(texts)])
    return [w for i, w in enumerate(words) if i not in skip]


def _x0(word: Word) -> int:
    return word.box.x0 if word.box is not None else 0


def _header_prefix(texts: list[str]) -> int:
    """Сколько слов в начале строки — заголовок упражнения («Упр. 245», «№ 245», «245.»)."""
    count = 0
    after_keyword = False
    for text in texts:
        if _HEADER_KEYWORD.match(text):
            after_keyword, count = True, count + 1
            continue
        if _NUMBER_DOT.match(text) or (after_keyword and _NUMBER_PLAIN.match(text)):
            after_keyword, count = False, count + 1
            continue
        return count
    return count


def _previous_actual(pairs: list[Pair], idx: int, words: list[Word]) -> str | None:
    """Последнее слово, которое ребёнок правда написал перед пропуском (по выравниванию) — не
    слово эталона: иначе подсказка называет ребёнку то самое слово, которое он не написал."""
    for prev in reversed(pairs[:idx]):
        if prev.kind in ("match", "subst") and prev.actual_index is not None:
            return words[prev.actual_index].text
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
        return Finding(
            task_index=task_index,
            kind="spelling",
            strength="candidate",
            expected=expected[pair.expected_index],
            actual=word.text,
            word=word,
            line=(word.line or 0) + 1,
            detail=KIND_DETAIL["spelling"].format(actual=word.text),
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
    return Finding(
        task_index=task_index,
        kind="extra_word",
        strength="candidate",
        actual=word.text,
        word=word,
        line=(word.line or 0) + 1,
        detail=KIND_DETAIL["extra_word"].format(actual=word.text),
    )
