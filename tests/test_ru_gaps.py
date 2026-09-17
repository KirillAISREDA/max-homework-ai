"""Заполнение пропусков «вставь буквы» по словарю: один кандидат → verified, иначе LLM."""

import json

import pytest

from conftest import FakeLLMClient
from hwcheck.subjects.russian.gaps import (
    MAX_SLOTS,
    Dictionary,
    HunspellDictionary,
    derive_text,
    fill_gap,
    find_gaps,
    tokenize,
)


class SetDictionary:
    def __init__(self, *words: str) -> None:
        self._words = set(words)

    def lookup(self, word: str) -> bool:
        return word in self._words


WORDS: Dictionary = SetDictionary(
    "машина", "щука", "щека", "лиса", "леса", "сделать", "с", "делать", "погода", "поздняя"
)


def test_tokenize_keeps_gaps_and_brackets_drops_punctuation() -> None:
    assert tokenize("Наступила п_здняя осень. (С)делать — быстро, м_шина!") == [
        "Наступила", "п_здняя", "осень", "(С)делать", "быстро", "м_шина"
    ]  # fmt: skip


def test_fill_gap_single_and_ambiguous() -> None:
    assert fill_gap("м_шина", WORDS) == ["машина"]
    assert sorted(fill_gap("щ_ка", WORDS)) == ["щека", "щука"]
    assert fill_gap("х_х", WORDS) == []
    assert fill_gap("_" * (MAX_SLOTS + 1) + "а", WORDS) == []  # перебор 33^4 не делаем


def test_fill_gap_keeps_case_of_pattern() -> None:
    assert fill_gap("М_шина", WORDS) == ["Машина"]


def test_brackets_joined_or_separate() -> None:
    [gap] = find_gaps(["(с)делать"], WORDS)
    assert sorted(gap.candidates) == ["с делать", "сделать"]  # предлог + слово или приставка


def test_brackets_only_for_prefix_pattern_not_any_parenthesised_token() -> None:
    # «(осень)» целиком в скобках — обычное слово, не «(предлог)слово»: не пропуск
    assert find_gaps(["(осень)", "лес"], WORDS) == []


async def test_derive_text_verified_when_all_gaps_single() -> None:
    derived = await derive_text("Наступила п_здняя осень.", WORDS, None, model="m")
    assert derived.words == ["Наступила", "поздняя", "осень"]
    assert (derived.gap_indices, derived.trust, derived.derived_by) == (
        [1],
        "verified",
        "dictionary",
    )


async def test_derive_text_without_gaps_is_unverified() -> None:
    """Пропусков нет — словарь ничего не проверял: «списать текст» не эталон `verified`, иначе
    ответ уходит в базу знаний со статусом «проверено словарём» без единой проверки (ревью, I4)."""
    derived = await derive_text("Наступила поздняя осень.", WORDS, None, model="m")
    assert derived.words == ["Наступила", "поздняя", "осень"]
    assert derived.gap_indices == []
    assert (derived.trust, derived.derived_by) == ("unverified", "dictionary")


async def test_derive_text_asks_llm_for_ambiguous_and_is_unverified() -> None:
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 3, "word": "леса"}]})])
    derived = await derive_text("За дальние л_са несёт м_шина.", WORDS, llm, model="m")
    assert derived.words == ["За", "дальние", "леса", "несёт", "машина"]
    assert derived.trust == "unverified" and derived.derived_by == "llm:m@v1"
    prompt = llm.calls[0][1].content
    assert "л_са" in prompt and "леса" in prompt and "лиса" in prompt  # кандидаты — из словаря


async def test_derive_text_llm_must_pick_from_candidates() -> None:
    # index 1 — валидный (1-based) номер пропуска «щ_ка»; слово не из кандидатов — не берём
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 1, "word": "щёки"}]})])
    derived = await derive_text("щ_ка", WORDS, llm, model="m")
    assert derived.words == ["щ_ка"] and derived.unresolved == [0]  # чужое слово не берём


async def test_derive_text_llm_unknown_index_is_ignored() -> None:
    # index 99 не соответствует ни одному пропуску — ответ игнорируется, даже если слово словарное
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 99, "word": "щука"}]})])
    derived = await derive_text("щ_ка", WORDS, llm, model="m")
    assert derived.words == ["щ_ка"] and derived.unresolved == [0]


async def test_derive_text_llm_free_answer_must_fit_pattern_and_dictionary() -> None:
    # 4 слота — сверх MAX_SLOTS, словарь кандидатов не перебирает (0 кандидатов); свободный ответ
    # LLM принимается, только если совпадает по длине с шаблоном и есть в словаре
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 1, "word": "лиса"}]})])
    derived = await derive_text("____", WORDS, llm, model="m")
    assert derived.words == ["лиса"]
    assert derived.unresolved == []
    assert derived.trust == "unverified"


async def test_derive_text_llm_free_answer_rejected_if_length_does_not_fit() -> None:
    # «машина» словарное, но не подходит под длину шаблона «х_х» — не берём
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 1, "word": "машина"}]})])
    derived = await derive_text("х_х", WORDS, llm, model="m")
    assert derived.words == ["х_х"] and derived.unresolved == [0]


async def test_derive_text_without_llm_leaves_pattern() -> None:
    derived = await derive_text("щ_ка плывёт", WORDS, None, model="m")
    assert derived.words == ["щ_ка", "плывёт"] and derived.trust == "unverified"
    assert derived.unresolved == [0]
    assert derived.derived_by == "dictionary"  # llm не вызывался — эталон не помечаем как llm:...


@pytest.mark.slow
def test_real_hunspell_dictionary_loads() -> None:
    dictionary = HunspellDictionary.load()
    assert dictionary.lookup("машина") and not dictionary.lookup("машына")
