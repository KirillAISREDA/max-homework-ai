"""Русский через контракт SubjectModule: фейки vision, OCR и базы знаний."""

import json

import pytest

from hwcheck.bot.check import CheckModels
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.ocr_client import OcrError
from hwcheck.subjects.base import Box, SubjectTask, Word
from hwcheck.subjects.kb_models import KbRule
from hwcheck.subjects.registry import SubjectDeps, module_for
from hwcheck.subjects.russian.module import RussianModule
from test_ru_gaps import WORDS
from test_ru_recognize import NOTEBOOK, TEXTBOOK, FakeVision, _jpeg

MODELS = CheckModels(vision="v", structure="s", solver="m")


class FakeOcr:
    def __init__(self, words: list[Word] | None) -> None:
        self._words = words

    async def recognize(self, image: bytes) -> list[Word]:
        if self._words is None:
            raise OcrError("ocr: ConnectError")
        return self._words


def _w(text: str, i: int, line: int = 0) -> Word:
    return Word(text=text, box=Box(x0=i * 40, y0=line * 20, x1=i * 40 + 30, y1=line * 20 + 15),
                confidence=0.8, line=line)  # fmt: skip


async def test_recognize_textbook_and_notebook() -> None:
    module = RussianModule(FakeVision([TEXTBOOK, NOTEBOOK]), MODELS, ocr=FakeOcr([_w("осень", 0)]),
                           dictionary=WORDS)  # fmt: skip
    textbook = await module.recognize(_jpeg())
    assert textbook.role == "textbook" and textbook.tasks[0].condition == "Наступила п_здняя ос_нь."
    notebook = await module.recognize(_jpeg())
    assert notebook.role == "notebook" and notebook.tasks[0].words[0].text == "осень"
    assert notebook.usage.calls == 1  # vision один раз; OCR не считается вызовом LLM


async def test_recognize_notebook_when_ocr_fails() -> None:
    module = RussianModule(FakeVision([NOTEBOOK]), MODELS, ocr=FakeOcr(None), dictionary=WORDS)
    page = await module.recognize(_jpeg())
    assert page.role == "notebook" and page.failure == "ocr_failed" and page.tasks == []


async def test_recognize_notebook_without_ocr_client() -> None:
    module = RussianModule(FakeVision([NOTEBOOK]), MODELS, ocr=None, dictionary=WORDS)
    page = await module.recognize(_jpeg())
    assert page.failure == "ocr_failed"


async def test_resolve_reference_saves_page_and_dictionary_answer() -> None:
    kb = InMemoryKnowledgeBase()
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="245", condition="Наступила п_здняя осень.", photo_path="p.jpg")
    [reference] = await module.resolve_reference([task], kb)
    assert (reference.origin, reference.trust) == ("derived", "verified")
    assert reference.payload["words"] == ["Наступила", "поздняя", "осень"]
    page = await kb.find_page("russian", "Наступила п_здняя осень.")
    assert (
        page is not None
        and page.photo_path == "p.jpg"
        and page.tasks[0].task_kind == "fill_letters"
    )
    [answer] = await kb.answers_for(page.tasks[0].id or 0)
    assert (answer.derived_by, answer.status) == ("dictionary", "verified")
    # второй раз — из базы, без пересчёта
    [again] = await module.resolve_reference([task], kb)
    assert again.origin == "kb" and again.trust == "verified"


async def test_resolve_reference_ambiguous_goes_to_llm_and_review_queue() -> None:
    kb = InMemoryKnowledgeBase()
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"choices": [{"index": 1, "word": "щука"}]})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    [reference] = await module.resolve_reference([SubjectTask(number="1", condition="щ_ка")], kb)
    assert reference.trust == "unverified" and reference.payload["words"] == ["щука"]
    [(_, answer)] = await kb.unverified_answers("russian", 10)
    assert answer.derived_by == "llm:s@v1"


async def test_resolve_reference_without_kb_still_derives() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="м_шина")], kb=None
    )
    assert reference.payload["words"] == ["машина"]


async def test_check_produces_candidate_findings_and_sentence_payload() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="Наступила п_здняя осень.")], kb=None
    )
    [result] = await module.check([task], [reference])
    [finding] = result.findings
    assert (finding.kind, finding.strength, finding.actual) == ("spelling", "candidate", "позняя")
    assert result.payload["sentence"]["позняя"] == "Наступила позняя осень"


async def test_check_without_reference_is_uncertain() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("осень", 0)])
    [result] = await module.check([task], [])
    [finding] = result.findings
    assert (
        finding.kind == "uncertain"
        and finding.detail == "нет текста упражнения — пришли фото учебника"
    )


async def test_start_tutoring_builds_word_session_with_rule() -> None:
    kb = InMemoryKnowledgeBase()
    await kb.add_rule(
        KbRule(code="ru.orth.unstressed_vowel", subject="russian", grade_from=2,
               title="Безударная гласная в корне", statement="Подбери проверочное слово.",
               example="лесá — лес", finding_kinds=["spelling"])
    )  # fmt: skip
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.9})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="Наступила п_здняя осень.")], kb=None
    )
    [result] = await module.check([task], [reference])
    confirmed = result.model_copy(
        update={"findings": [result.findings[0].model_copy(update={"confirmed": True})]}
    )
    session = await module.start_tutoring(confirmed, task, kb)
    assert session.word is not None and session.word.expected == "поздняя"
    assert session.word.rule_title == "Безударная гласная в корне"
    assert session.ref.answer == "поздняя" and session.expected == "поздняя"


async def test_start_tutoring_without_error_raises() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("осень", 0)])
    [result] = await module.check([task], [])
    with pytest.raises(ValueError, match="нет подтверждённой ошибки"):
        await module.start_tutoring(result, task, kb=None)


def test_registry_russian() -> None:
    deps = SubjectDeps(llm=FakeVision([]), models=MODELS, cache=None, dictionary=WORDS)  # type: ignore[arg-type]
    assert isinstance(module_for("russian", deps), RussianModule)
