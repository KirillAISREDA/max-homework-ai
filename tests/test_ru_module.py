"""Русский через контракт SubjectModule: фейки vision, OCR и базы знаний."""

import io
import json

import pytest
from PIL import Image

from hwcheck.bot.check import CheckModels
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.ocr_client import OcrError
from hwcheck.subjects.base import (
    Box,
    Finding,
    NoTutorableFinding,
    Reference,
    SubjectTask,
    TaskResult,
    Word,
)
from hwcheck.subjects.kb_models import KbRule
from hwcheck.subjects.registry import SubjectDeps, module_for
from hwcheck.subjects.russian.module import NO_PAIR_DETAIL, RussianModule
from test_ru_gaps import WORDS
from test_ru_recognize import NOTEBOOK, TEXTBOOK, FakeVision, _jpeg

MODELS = CheckModels(vision="v", structure="s", solver="m")


class FakeOcr:
    def __init__(self, words: list[Word] | None) -> None:
        self._words = words
        self.images: list[bytes] = []  # кадры, которые дошли до OCR: их сверяет тест поворота

    async def recognize(self, image: bytes) -> list[Word]:
        self.images.append(image)
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


async def test_recognize_rotates_the_photo_for_ocr() -> None:
    """Фото боком: vision узнал тетрадь только в повёрнутой ориентации, а OCR получал исходные
    байты — страница превращалась в мусор (живой прогон 18.09, ru-2: 184 слова)."""
    unknown = json.dumps({"role": "unknown", "exercises": []})
    image = _jpeg(40, 20)
    ocr = FakeOcr([_w("осень", 0)])
    module = RussianModule(FakeVision([unknown, NOTEBOOK]), MODELS, ocr=ocr, dictionary=WORDS)

    page = await module.recognize(image)

    assert page.role == "notebook" and page.rotation == 270
    [sent] = ocr.images
    assert sent != image
    with Image.open(io.BytesIO(sent)) as rotated:
        assert rotated.size == (20, 40)  # кадр повёрнут, а не просто пережат


async def test_recognize_without_rotation_gives_ocr_the_original_photo() -> None:
    image = _jpeg(40, 20)
    ocr = FakeOcr([_w("осень", 0)])
    module = RussianModule(FakeVision([NOTEBOOK]), MODELS, ocr=ocr, dictionary=WORDS)

    page = await module.recognize(image)

    assert page.rotation == 0 and ocr.images == [image]


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


async def test_resolve_reference_text_without_gaps_is_not_checked_by_dictionary() -> None:
    """«Спиши текст» без пропусков: словарь ничего не проверял — ответ в базе не `verified` и
    ждёт ревьюера, а не выдаёт себя за проверенный словарём (ревью 17.09, I4)."""
    kb = InMemoryKnowledgeBase()
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="245", condition="Наступила поздняя осень.")
    [reference] = await module.resolve_reference([task], kb)
    assert reference.trust == "unverified"
    page = await kb.find_page("russian", "Наступила поздняя осень.")
    assert page is not None
    [answer] = await kb.answers_for(page.tasks[0].id or 0)
    assert (answer.derived_by, answer.checked_by, answer.status) == (
        "dictionary",
        None,
        "unverified",
    )


async def test_resolve_reference_ambiguous_goes_to_llm_and_review_queue() -> None:
    kb = InMemoryKnowledgeBase()
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"choices": [{"index": 1, "word": "щука"}]})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    [reference] = await module.resolve_reference([SubjectTask(number="1", condition="щ_ка")], kb)
    assert reference.trust == "unverified" and reference.payload["words"] == ["щука"]
    [(_, answer)] = await kb.unverified_answers("russian", 10)
    assert answer.derived_by == "llm:s@v1"


async def test_resolve_reference_rejected_answer_is_not_resaved() -> None:
    kb = InMemoryKnowledgeBase()
    llm = FakeVision([])
    llm.chat_responses = [
        json.dumps({"choices": [{"index": 1, "word": "щука"}]}),
        json.dumps({"choices": [{"index": 1, "word": "щука"}]}),
    ]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", condition="щ_ка")
    [reference] = await module.resolve_reference([task], kb)
    assert reference.origin == "derived"
    page = await kb.find_page("russian", "щ_ка")
    assert page is not None and page.tasks[0].id is not None
    task_id = page.tasks[0].id
    [answer] = await kb.answers_for(task_id)
    await kb.set_answer_status(answer.id or 0, "rejected", "manual")
    # ревьюер отклонил единственный ответ — модуль не должен молча пересоздать его
    [again] = await module.resolve_reference([task], kb)
    assert again.origin == "derived"
    assert await kb.unverified_answers("russian", 10) == []
    assert len(await kb.answers_for(task_id)) == 1


class RaisingKb:
    async def find_page(self, subject: str, text: str) -> object:
        raise RuntimeError("kb недоступна")


async def test_resolve_reference_kb_read_failure_falls_back_to_derive() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", condition="м_шина")
    [reference] = await module.resolve_reference([task], RaisingKb())  # type: ignore[arg-type]
    assert reference.payload["words"] == ["машина"]


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


async def _three_references(module: RussianModule) -> list[Reference]:
    return await module.resolve_reference(
        [
            SubjectTask(number="245", condition="Наступила поздняя осень."),
            SubjectTask(number="246", condition="Щука плывёт в реке."),
            SubjectTask(number="247", condition="Машина едет по дороге."),
        ],
        kb=None,
    )


async def test_check_pairs_unnumbered_notebook_by_word_overlap() -> None:
    """Номер над работой ребёнок не подписал, а на фото учебника — три упражнения: пару даёт
    совпадение слов, а не «нет текста упражнения — пришли фото учебника» (ревью 17.09, I2)."""
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    references = await _three_references(module)
    task = SubjectTask(
        number="1", number_on_page=False,
        words=[_w("Щука", 0), _w("плывёт", 1), _w("в", 2), _w("реки", 3)],
    )  # fmt: skip
    [result] = await module.check([task], references)
    assert result.reference is not None and result.reference.task_number == "246"
    [finding] = result.findings
    assert (finding.kind, finding.actual, finding.expected) == ("spelling", "реки", "реке")


async def test_check_unnumbered_notebook_without_a_pair_asks_for_the_number() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    references = await _three_references(module)
    task = SubjectTask(
        number="1", number_on_page=False,
        words=[_w("совсем", 0), _w("другой", 1), _w("текст", 2)],
    )  # fmt: skip
    [result] = await module.check([task], references)
    [finding] = result.findings
    assert (finding.kind, finding.detail) == ("uncertain", NO_PAIR_DETAIL)
    assert result.reference is None


async def test_check_does_not_pair_two_auto_numbers_by_number_alone() -> None:
    """Обе стороны с присвоенным номером «1» — совпадение номеров ничего не значит: пару
    ищем по словам, и если её нет, просим подписать номер."""
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    references = await module.resolve_reference(
        [
            SubjectTask(number="1", number_on_page=False, condition="Наступила поздняя осень."),
            SubjectTask(number="2", number_on_page=False, condition="Щука плывёт в реке."),
        ],
        kb=None,
    )
    task = SubjectTask(
        number="1", number_on_page=False,
        words=[_w("Машина", 0), _w("едет", 1), _w("по", 2), _w("дороге", 3)],
    )  # fmt: skip
    [result] = await module.check([task], references)
    assert [f.detail for f in result.findings] == [NO_PAIR_DETAIL]


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


async def test_start_tutoring_skips_confirmed_missing_word_picks_spelling() -> None:
    # находки в пропуске (сюда попадают все типы) сортируются первыми check_words — confirmed
    # missing_word (без actual) не должен блокировать разбор следующей за ней spelling-находки
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.9})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    missing = Finding(
        task_index=0, kind="missing_word", strength="candidate", expected="нас", confirmed=True
    )
    spelling = Finding(
        task_index=0, kind="spelling", strength="candidate", expected="поздняя",
        actual="позняя", word=_w("позняя", 1), confirmed=True,
    )  # fmt: skip
    result = TaskResult(
        task_index=0,
        findings=[missing, spelling],
        payload={"sentence": {"позняя": "Наступила позняя осень"}},
    )
    session = await module.start_tutoring(result, task, kb=None)
    assert session.word is not None and session.word.expected == "поздняя"
    assert session.word.actual == "позняя"


async def test_start_tutoring_without_error_raises() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("осень", 0)])
    [result] = await module.check([task], [])
    # свой тип исключения: бот отличает «нечего разбирать» от сбоя модуля (ревью R7)
    with pytest.raises(NoTutorableFinding, match="нет подтверждённой ошибки"):
        await module.start_tutoring(result, task, kb=None)


def test_registry_russian() -> None:
    deps = SubjectDeps(llm=FakeVision([]), models=MODELS, cache=None, dictionary=WORDS)  # type: ignore[arg-type]
    assert isinstance(module_for("russian", deps), RussianModule)
