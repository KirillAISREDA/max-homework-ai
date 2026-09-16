"""Спайк: сколько пропусков «вставь букву» закрывает словарь Hunspell без LLM."""

import itertools
import json
import re
import sys
from pathlib import Path

from spylls.hunspell import Dictionary

LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
GAP = re.compile(r"[а-яё]*_+[а-яё_]*", re.IGNORECASE)


def fill_gap(word: str, dictionary: Dictionary) -> list[str]:
    """Кандидаты: словарные слова, совпадающие с шаблоном; «_» — одна буква."""
    slots = word.count("_")
    found: list[str] = []
    for letters in itertools.product(LETTERS, repeat=slots):
        candidate = word
        for letter in letters:
            candidate = candidate.replace("_", letter, 1)
        if dictionary.lookup(candidate.lower()):
            found.append(candidate)
    return found


def main() -> None:
    dictionary = Dictionary.from_files("spikes/hunspell_gaps/ru_RU")
    rows = json.loads(Path("spikes/hunspell_gaps/exercises.json").read_text(encoding="utf-8"))
    single = ambiguous = none = wrong = 0
    for row in rows:
        answers = row["answer"].split()
        for token, expected in zip(row["text"].split(), answers, strict=True):
            if "_" not in token:
                continue
            candidates = fill_gap(token.strip(".,!?"), dictionary)
            expected = expected.strip(".,!?")
            if len(candidates) == 1:
                single += 1
                if candidates[0].lower() != expected.lower():
                    wrong += 1
                    print("НЕВЕРНО:", token, candidates, expected)
            elif candidates:
                ambiguous += 1
                print("НЕОДНОЗНАЧНО:", token, candidates, "| ожидалось:", expected)
            else:
                none += 1
                print("НЕТ:", token, expected)
    total = single + ambiguous + none
    print(
        f"один кандидат: {single}/{total} (неверных {wrong}); несколько: {ambiguous}; ноль: {none}"
    )


if __name__ == "__main__":
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
