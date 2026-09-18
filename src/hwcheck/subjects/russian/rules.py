"""Орфограммы 1–4 класса: коды карточек `kb_rules`, классификатор пары «написано → верно»,
загрузка карточек из assets/kb/rules_russian.json (`hwcheck kb load-rules`).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.prompts import load_prompt
from hwcheck.subjects.kb_models import KbRule

MIN_CONFIDENCE = 0.6
RULE_CODES: dict[str, str] = {
    "ru.orth.unstressed_vowel": "Безударная гласная в корне, проверяемая ударением",
    "ru.orth.unstressed_vowel_dict": "Непроверяемая безударная гласная (словарное слово)",
    "ru.orth.paired_consonant": "Парная согласная в корне и на конце слова",
    "ru.orth.silent_consonant": "Непроизносимая согласная в корне",
    "ru.orth.zhi_shi": "Сочетания жи–ши, ча–ща, чу–щу",
    "ru.orth.chk_chn": "Сочетания чк, чн, нч, щн без мягкого знака",
    "ru.orth.soft_sign": "Мягкий знак — показатель мягкости",
    "ru.orth.separating_sign": "Разделительные ь и ъ",
    "ru.orth.double_consonant": "Удвоенные согласные",
    "ru.orth.capital": "Заглавная буква в именах собственных и начале предложения",
    "ru.orth.prefix": "Приставки пишутся слитно; приставка и предлог",
    "ru.orth.preposition": "Предлог пишется отдельно от слова",
    "ru.orth.prefix_vowel": "Гласные и согласные в приставках (по-, за-, от-, под-)",
    "ru.orth.suffix": "Суффиксы -ик/-ек, -оньк/-еньк",
    "ru.orth.noun_ending": "Безударные падежные окончания существительных",
    "ru.orth.adj_ending": "Безударные окончания прилагательных",
    "ru.orth.verb_ending": "Безударные личные окончания глаголов",
    "ru.orth.tsya": "-тся и -ться в глаголах",
    "ru.orth.ne_verb": "Не с глаголами",
    "ru.orth.hissing_soft": "Мягкий знак после шипящих на конце существительных и глаголов",
}


class _Orthogram(BaseModel):
    rule_code: str
    confidence: float = Field(ge=0.0, le=1.0)


async def classify_orthogram(
    llm: LLMClient, actual: str, expected: str, sentence: str, *, model: str, version: str = "v1"
) -> str | None:
    """Код орфограммы или None (неуверен / неизвестный код) — тьютор тогда без карточки."""
    codes = "\n".join(f"- {code}: {title}" for code, title in RULE_CODES.items())
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_orthogram", version)),
        ChatMessage(
            role="user",
            content=f"Написано: {actual}\nВерно: {expected}\nПредложение: {sentence}\n\n"
            f"Коды орфограмм:\n{codes}",
        ),
    ]
    try:
        answer, _ = await chat_structured(llm, messages, _Orthogram, model=model)
    except StructuredOutputError:
        return None
    if answer.rule_code not in RULE_CODES or answer.confidence < MIN_CONFIDENCE:
        return None
    return answer.rule_code


def load_rules(path: Path) -> list[KbRule]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("load-rules: ожидается JSON-список карточек")
    return [KbRule.model_validate(item) for item in data]
