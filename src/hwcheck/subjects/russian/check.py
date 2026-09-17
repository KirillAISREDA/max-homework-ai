"""Находки по выравниванию (спецификация §6): все — `candidate`, ребёнок подтверждает
«здесь написано …?». В пропуске — первыми, затем по уверенности OCR (уверенное расхождение
вероятнее ошибка ученика, неуверенное — шум распознавания)."""

from __future__ import annotations

from hwcheck.subjects.base import Finding, SubjectTask, Word
from hwcheck.subjects.russian.align import Pair, align
from hwcheck.subjects.russian.gaps import DerivedText
from hwcheck.subjects.russian.recognize import header_number

MAX_DIFF_SHARE = 0.4  # больше расхождений — это не то упражнение или не та страница
KIND_DETAIL = {
    "spelling": "проверь слово «{actual}»",
    "missing_word": "кажется, пропущено слово после «{previous}»",
    "extra_word": "лишнее слово «{actual}»?",
}
UNCERTAIN_DETAIL = "не смог сверить с упражнением"


def check_words(task_index: int, task: SubjectTask, derived: DerivedText) -> list[Finding]:
    words = _body_words(task)
    expected = derived.words
    if not words or not expected:
        return []
    pairs = align(expected, [w.text for w in words])
    # доля расхождений считаем только по «пропущено/лишнее» — структурным различиям, которые и
    # означают чужой текст; описок может быть сколько угодно, это не повод считать текст не тем
    structural = sum(1 for p in pairs if p.kind in ("missing", "extra"))
    if structural / (len(expected) + len(words)) > MAX_DIFF_SHARE:
        return [
            Finding(
                task_index=task_index,
                kind="uncertain",
                strength="candidate",
                detail=UNCERTAIN_DETAIL,
            )
        ]
    diffs = [p for p in pairs if p.kind != "match"]
    unresolved = set(derived.unresolved)
    gaps = set(derived.gap_indices)
    scored = [
        (pair.expected_index in gaps, _finding(task_index, pair, expected, words))
        for pair in diffs
        if pair.expected_index not in unresolved
    ]
    # в пропуске → первыми; орфография раньше пропущенных/лишних слов; уверенное OCR раньше шума
    scored.sort(key=lambda item: (not item[0], item[1].kind != "spelling", -_confidence(item[1])))
    return [finding for _, finding in scored]


def _confidence(finding: Finding) -> float:
    # пропущенному слову не с чем сверять OCR-уверенность — это не шум распознавания, а
    # установленный факт (в тексте ученика слова нет), поэтому он не «неувереннее» описки
    if finding.word is None:
        return 1.0
    return finding.word.confidence if finding.word.confidence is not None else 0.0


def _body_words(task: SubjectTask) -> list[Word]:
    """Без строки-заголовка «Упражнение 245.» — её нет в тексте упражнения."""
    first_line = [w for w in task.words if (w.line or 0) == (task.words[0].line or 0)]
    header = task.number_on_page and header_number([" ".join(w.text for w in first_line)])
    return [w for w in task.words if not (header and w in first_line)]


def _finding(task_index: int, pair: Pair, expected: list[str], words: list[Word]) -> Finding:
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
        previous = expected[pair.expected_index - 1] if pair.expected_index else "начала"
        return Finding(
            task_index=task_index,
            kind="missing_word",
            strength="candidate",
            expected=expected[pair.expected_index],
            detail=KIND_DETAIL["missing_word"].format(previous=previous),
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
