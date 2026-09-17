"""Эталон для «вставь буквы / раскрой скобки» (спецификация §6, спайк Hunspell 17.09).

Пропуск заполняется словарём: ровно одно словарное слово под шаблон → эталон `verified`
(`derived_by=dictionary`); несколько или ноль → GigaChat выбирает из словарных кандидатов по
контексту предложения → `unverified`, очередь `hwcheck kb review`. LLM не придумывает слово —
только выбирает из списка (иначе кандидат отбрасывается).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Hashable
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import Trust

ASSETS = Path(__file__).resolve().parents[4] / "assets" / "hunspell"
LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
MAX_SLOTS = 3  # 33³ = 35 937 обращений к словарю; 4 слота — уже миллион, не перебираем
# предлоги, которые встречаются в «раскрой скобки»: (с)делать, (в)лесу, (по)дороге
PREPOSITIONS = frozenset(
    [
        "с", "со", "в", "во", "на", "по", "за", "из", "от", "до", "под", "над", "о", "об", "у",
        "к", "ко", "при", "без", "про", "через", "для", "между",
    ]
)  # fmt: skip
_TOKEN = re.compile(r"[А-Яа-яЁё_()]+(?:-[А-Яа-яЁё_()]+)*")
_BRACKET = re.compile(r"^\(([А-Яа-яЁё]+)\)([А-Яа-яЁё]+)$")


class Dictionary(Hashable, Protocol):
    # Hashable — для lru_cache в `_fill`: кэш по (pattern, dictionary); словарь один на процесс,
    # хэш по идентичности объекта (свой __hash__ реализации не переопределяют)
    def lookup(self, word: str) -> bool: ...


class HunspellDictionary:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    @classmethod
    def load(cls, path: Path = ASSETS / "ru_RU") -> HunspellDictionary:
        from spylls.hunspell import Dictionary as Spylls  # секунды на загрузку — один раз

        return cls(Spylls.from_files(str(path)))

    def lookup(self, word: str) -> bool:
        return bool(self._inner.lookup(word))  # type: ignore[attr-defined]


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


class Gap(BaseModel):
    index: int  # позиция слова в tokenize(text)
    pattern: str
    candidates: list[str] = Field(default_factory=list)


def fill_gap(pattern: str, dictionary: Dictionary) -> list[str]:
    """Кандидаты под шаблон `м_шина`: по одной букве на каждый `_`; регистр шаблона сохраняется."""
    return list(_fill(pattern, dictionary))


@lru_cache(maxsize=4096)
def _fill(pattern: str, dictionary: Dictionary) -> tuple[str, ...]:
    slots = pattern.count("_")
    if slots == 0 or slots > MAX_SLOTS:
        return ()
    found: list[str] = []
    for letters in itertools.product(LETTERS, repeat=slots):
        candidate = pattern
        for letter in letters:
            candidate = candidate.replace("_", letter, 1)
        if dictionary.lookup(candidate.lower()):
            found.append(candidate)
    return tuple(found)


def _bracket_candidates(token: str, dictionary: Dictionary) -> list[str]:
    match = _BRACKET.match(token)
    if match is None:
        return []
    prefix, rest = match.group(1), match.group(2)
    candidates = []
    if dictionary.lookup((prefix + rest).lower()):
        candidates.append(prefix + rest)
    if prefix.lower() in PREPOSITIONS and dictionary.lookup(rest.lower()):
        candidates.append(f"{prefix} {rest}")
    return candidates


def find_gaps(words: list[str], dictionary: Dictionary) -> list[Gap]:
    gaps = []
    for index, word in enumerate(words):
        if "_" in word:
            gaps.append(Gap(index=index, pattern=word, candidates=fill_gap(word, dictionary)))
        elif "(" in word:
            gaps.append(
                Gap(index=index, pattern=word, candidates=_bracket_candidates(word, dictionary))
            )
    return gaps


class DerivedText(BaseModel):
    words: list[str]  # слова эталона; неразрешённый пропуск остаётся шаблоном
    gap_indices: list[int]  # позиции в `words`, где был пропуск (для приоритета находок)
    trust: Trust
    derived_by: str  # dictionary | llm:<модель>@<версия промпта>
    unresolved: list[int] = Field(default_factory=list)  # позиции, оставшиеся шаблоном


class _Choice(BaseModel):
    index: int  # 1-based номер слова, как показан LLM в listing (не совпадает с Gap.index)
    word: str


class _Choices(BaseModel):
    choices: list[_Choice]


async def derive_text(
    text: str,
    dictionary: Dictionary,
    llm: LLMClient | None,
    *,
    model: str,
    prompt_version: str = "v1",
) -> DerivedText:
    words = tokenize(text)
    gaps = find_gaps(words, dictionary)
    chosen: dict[int, str] = {g.index: g.candidates[0] for g in gaps if len(g.candidates) == 1}
    ambiguous = [g for g in gaps if len(g.candidates) != 1]
    if ambiguous:
        # маркируем как llm-эталон, даже если llm недоступен или ничего не разрешил — раз есть
        # неоднозначный пропуск, эталон в любом случае не проверен детерминированно словарём
        derived_by = f"llm:{model}@{prompt_version}"
        if llm is not None:
            chosen |= await _ask_llm(
                llm, words, ambiguous, dictionary, model=model, version=prompt_version
            )
    else:
        derived_by = "dictionary"
    filled = [chosen.get(i, w) for i, w in enumerate(words)]
    unresolved = [g.index for g in gaps if g.index not in chosen]
    trust: Trust = "verified" if not ambiguous else "unverified"
    return DerivedText(
        words=filled,
        gap_indices=[g.index for g in gaps],
        trust=trust,
        derived_by=derived_by,
        unresolved=unresolved,
    )


async def _ask_llm(
    llm: LLMClient,
    words: list[str],
    gaps: list[Gap],
    dictionary: Dictionary,
    *,
    model: str,
    version: str,
) -> dict[int, str]:
    # LLM видит номера слов с 1 (человекочитаемо и надёжнее для модели, чем 0-based); при разборе
    # ответа переводим обратно в 0-based индексы `words`/`Gap.index`
    listing = "\n".join(
        f"{g.index + 1}: {g.pattern} — варианты: {', '.join(g.candidates) or 'словарь не нашёл'}"
        for g in gaps
    )
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_gaps", version)),
        ChatMessage(role="user", content=f"Текст: {' '.join(words)}\n\nПропуски:\n{listing}"),
    ]
    try:
        answer, _ = await chat_structured(llm, messages, _Choices, model=model)
    except StructuredOutputError:
        return {}
    allowed = {g.index: set(g.candidates) for g in gaps}
    chosen: dict[int, str] = {}
    for choice in answer.choices:
        index = choice.index - 1
        candidates = allowed.get(index)
        if candidates is None:
            continue
        # без словарных кандидатов LLM восстанавливает слово свободно — но только словарное
        if choice.word in candidates or (not candidates and dictionary.lookup(choice.word.lower())):
            chosen[index] = choice.word
    return chosen
