import json
from pathlib import Path

from conftest import FakeLLMClient
from hwcheck.subjects.kb_models import KbRule
from hwcheck.subjects.russian.rules import RULE_CODES, classify_orthogram, load_rules


async def test_classify_orthogram_returns_known_code() -> None:
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.8})])
    code = await classify_orthogram(llm, "машына", "машина", "Едет машына", model="s")
    assert code == "ru.orth.unstressed_vowel"
    assert "машына" in llm.calls[0][1].content and "машина" in llm.calls[0][1].content


async def test_classify_orthogram_unknown_or_unsure_is_none() -> None:
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.made_up", "confidence": 0.9})])
    assert await classify_orthogram(llm, "а", "б", "", model="s") is None
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.3})])
    assert await classify_orthogram(llm, "а", "б", "", model="s") is None


def test_rules_file_matches_codes(tmp_path: Path) -> None:
    rules = load_rules(Path("assets/kb/rules_russian.json"))
    assert {r.code for r in rules} == set(RULE_CODES)
    assert all(
        isinstance(r, KbRule) and r.subject == "russian" and r.grade_from >= 1 for r in rules
    )
