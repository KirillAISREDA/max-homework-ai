"""Выравнивание слов тетради с эталоном (спецификация §6): Левенштейн по словам, замена
оценивается буквенным сходством — похожее слово считается опиской, непохожее — парой
«пропущено + лишнее»."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

SIMILAR = 0.5  # доля совпавших букв, с которой слова считаются «тем же словом с опиской»
# «пропуск» и «лишнее» — раздельные операции (эталон недосчитался слова, у ученика — лишнее);
# вместе они должны стоить меньше 1.2 (непохожая замена), иначе для одиночной непохожей пары
# Левенштейн всегда предпочтёт грубую замену паре «пропуск + лишнее слово»
_GAP_COST = 0.5
_STRIP = re.compile(r"[^а-яa-z0-9-]")


def normalize(word: str) -> str:
    return _STRIP.sub("", word.lower().replace("ё", "е"))


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1 - levenshtein(a, b) / longest


class Pair(BaseModel):
    kind: Literal["match", "subst", "missing", "extra"]
    expected_index: int | None = None
    actual_index: int | None = None


def _subst_cost(expected: str, actual: str) -> float:
    if normalize(expected) == normalize(actual):
        return 0.0
    return 0.5 if similarity(expected, actual) >= SIMILAR else 1.2


def align(expected: list[str], actual: list[str]) -> list[Pair]:
    n, m = len(expected), len(actual)
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i * _GAP_COST
    for j in range(1, m + 1):
        cost[0][j] = j * _GAP_COST
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(
                cost[i - 1][j - 1] + _subst_cost(expected[i - 1], actual[j - 1]),
                cost[i - 1][j] + _GAP_COST,  # слово эталона пропущено
                cost[i][j - 1] + _GAP_COST,  # лишнее слово ученика
            )
    pairs: list[Pair] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            step = _subst_cost(expected[i - 1], actual[j - 1])
            if cost[i][j] == cost[i - 1][j - 1] + step:
                kind: Literal["match", "subst"] = "match" if step == 0 else "subst"
                pairs.append(Pair(kind=kind, expected_index=i - 1, actual_index=j - 1))
                i, j = i - 1, j - 1
                continue
        # при равенстве стоимостей «пропуск» и «лишнее» (одиночная непохожая пара) сперва
        # уводим «лишнее» — после разворота списка «пропуск» окажется раньше «лишнего»
        if j > 0 and cost[i][j] == cost[i][j - 1] + _GAP_COST:
            pairs.append(Pair(kind="extra", actual_index=j - 1))
            j -= 1
        else:
            pairs.append(Pair(kind="missing", expected_index=i - 1))
            i -= 1
    pairs.reverse()
    return pairs
