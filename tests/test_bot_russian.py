"""Русский в боте: альбом учебник + тетрадь → находки → «здесь написано …?» → разбор."""

import io
import json
from pathlib import Path

from PIL import Image

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import NOTHING_TO_TUTOR, SUBJECT_UNAVAILABLE, Bot
from hwcheck.bot.onboarding.router import CheckPhotos
from hwcheck.config import Settings
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.events import EventLog, read_events
from hwcheck.photos import PhotoStore
from hwcheck.subjects.registry import SubjectDeps
from test_bot import FakeMax
from test_ru_gaps import WORDS
from test_ru_module import MODELS, FakeOcr, _w
from test_ru_recognize import NOTEBOOK, TEXTBOOK, FakeVision


class Words:
    """Словарь этапа 2 плюс «осень»: без неё пропуск «ос_нь» из TEXTBOOK уходит в LLM, и тест
    проверял бы подбор слова, а не маршрутизацию бота (эталон для «ос_нь» тогда не собирается)."""

    def lookup(self, word: str) -> bool:
        return word == "осень" or WORDS.lookup(word)


DICTIONARY = Words()


def _jpeg(width: int = 200, height: int = 60) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


class RuFakeMax(FakeMax):
    """Распознавание страницы декодирует картинку и кропает слово — фейк отдаёт настоящий JPEG."""

    async def download(self, url: str) -> bytes:
        return _jpeg()


def _bot(
    tmp_path: Path, vision: FakeVision, ocr: FakeOcr, *, dictionary: object = DICTIONARY
) -> tuple[Bot, RuFakeMax, Path]:
    events_path = tmp_path / "events.jsonl"
    max_client = RuFakeMax()
    settings = Settings(
        _env_file=None, photos_dir=str(tmp_path / "photos"), kb_photos_dir=str(tmp_path / "kb")
    )
    deps = SubjectDeps(
        llm=vision,  # type: ignore[arg-type]
        models=MODELS,
        cache=None,
        ocr=ocr,  # type: ignore[arg-type]
        kb=InMemoryKnowledgeBase(),
        dictionary=dictionary,  # type: ignore[arg-type]
    )
    bot = Bot(
        max_client,  # type: ignore[arg-type]
        vision,  # type: ignore[arg-type]
        InMemoryStateStore(),
        EventLog(events_path, "dev"),
        settings,
        photos=PhotoStore(tmp_path / "photos", 30),
        kb_photos=PhotoStore(Path(settings.kb_photos_dir), settings.kb_photos_ttl_days),
        subjects=deps,
    )
    return bot, max_client, events_path


async def test_album_textbook_and_notebook_asks_about_word(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    ocr = FakeOcr([_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    bot, max_client, events_path = _bot(tmp_path, vision, ocr)

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")

    texts = [m.text for m in max_client.sent]
    assert any("уточню у тебя одну деталь" in t for t in texts)
    assert texts[-1] == "№245: здесь написано «позняя»?"
    assert max_client.sent[-1].image_token is not None  # кроп слова с фото №2 (photo_index=1)
    state = await bot._store.get(1)
    assert state.subject == "russian" and state.phase == "clarifying"
    assert state.tasks[0].grade is None and state.tasks[0].findings[0].word is not None
    assert state.tasks[0].findings[0].word.photo_index == 1
    assert [t.number for t in state.conditions] == ["245"]
    kinds = [e["type"] for e in read_events(events_path)]
    assert "reference_resolved" in kinds and "finding_created" in kinds


async def test_yes_confirms_error_and_tutor_starts(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    vision.chat_responses = [
        json.dumps({"rule_code": "ru.orth.silent_consonant", "confidence": 0.9}),
        json.dumps({"reply": "Какая орфограмма в этом слове?"}),
    ]
    ocr = FakeOcr([_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    bot, max_client, events_path = _bot(tmp_path, vision, ocr)
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")
    state = await bot._store.get(1)
    token = state.clarifications[0].token

    await bot._on_callback(1, 7, f"clarify:{token}:yes", "cb")
    assert max_client.sent[-1].text == "№245 — есть ошибка (слово «позняя») ❌"

    await bot._on_callback(1, 7, "tutor:0", "cb2")
    assert max_client.sent[-1].text == "Какая орфограмма в этом слове?"
    state = await bot._store.get(1)
    assert state.phase == "tutoring" and state.tutor is not None and state.tutor.word is not None
    events = [e for e in read_events(events_path)]
    kinds = [e["type"] for e in events]
    assert "finding_confirmed" in kinds and "orthogram_classified" in kinds
    classified = next(e for e in events if e["type"] == "orthogram_classified")
    assert classified["rule_code"] == "ru.orth.silent_consonant"


async def test_tutor_without_confirmed_error_says_nothing_to_discuss(tmp_path: Path) -> None:
    """Кнопка «Разобрать» из прошлой сводки, а находку ребёнок не подтверждал — не RETRY."""
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    ocr = FakeOcr([_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    bot, max_client, _ = _bot(tmp_path, vision, ocr)
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")

    await bot._on_callback(1, 7, "tutor:0", "cb")

    assert max_client.sent[-1].text == NOTHING_TO_TUTOR
    assert (await bot._store.get(1)).phase != "tutoring"


async def test_ocr_failure_reports_uncertain_and_event(tmp_path: Path) -> None:
    bot, max_client, events_path = _bot(tmp_path, FakeVision([TEXTBOOK, NOTEBOOK]), FakeOcr(None))

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")

    assert "Не смог прочитать тетрадь" in max_client.sent[-1].text
    assert "ocr_failed" in [e["type"] for e in read_events(events_path)]


async def test_textbook_only_is_remembered_for_next_message(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    ocr = FakeOcr([_w("Наступила", 0), _w("поздняя", 1), _w("осень", 2)])
    bot, max_client, _ = _bot(tmp_path, vision, ocr)

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="russian")
    assert max_client.sent[-1].text.startswith("Вижу страницу учебника (№245)")

    await bot._on_photo(chat_id=1, user_id=7, urls=["u2"], subject="russian")
    assert max_client.sent[-1].text == "Проверил! 1 из 1 верно.\n№245 — верно ✅"


async def test_textbook_photo_saved_for_knowledge_base(tmp_path: Path) -> None:
    """Страница учебника едет в базу знаний вместе с фото (TTL 365 дней, свой каталог)."""
    bot, _max_client, _ = _bot(tmp_path, FakeVision([TEXTBOOK]), FakeOcr([]))

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="russian")

    [condition] = (await bot._store.get(1)).conditions
    assert condition.photo_path is not None
    assert (tmp_path / "kb" / condition.photo_path).read_bytes() == _jpeg()


async def test_unconfigured_subject_tells_child_instead_of_failing(tmp_path: Path) -> None:
    """Предмет из профиля, а модуля в этом окружении нет (нет словаря): не сбой апдейта."""
    bot, max_client, _ = _bot(tmp_path, FakeVision([]), FakeOcr([]), dictionary=None)

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="russian")

    assert max_client.sent[-1].text == SUBJECT_UNAVAILABLE


async def test_math_path_unchanged_for_math_subject(tmp_path: Path) -> None:
    """subject=math идёт прежним кодом: FakeVision русского не вызывается."""
    bot, max_client, _ = _bot(tmp_path, FakeVision([]), FakeOcr([]))

    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="math")

    assert "Что-то пошло не так" in max_client.sent[-1].text  # math-vision не замокан → RETRY


def test_check_photos_carries_subject() -> None:
    assert (
        CheckPhotos(["u"]).subject == "math"
        and CheckPhotos(["u"], subject="russian").subject == "russian"
    )
