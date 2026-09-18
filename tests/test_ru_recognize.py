"""Русский: роль страницы и печатный текст — vision; тетрадь — слова OCR → задание."""

import io
import json
from collections.abc import Sequence

from PIL import Image

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.subjects.base import Box, Word
from hwcheck.subjects.russian.recognize import (
    FILLED_BY_HAND_COMMENT,
    ORIENTATIONS,
    RuExercise,
    RuPage,
    header_number,
    merge_hyphenation,
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)


def _jpeg(width: int = 8, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeVision:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.chat_responses: list[str] = []

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(content=self._responses.pop(0), model=model, tokens_in=7, tokens_out=3)

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        return LLMResult(content=self.chat_responses.pop(0), model=model, tokens_in=5, tokens_out=5)


TEXTBOOK = json.dumps(
    {
        "role": "textbook",
        "exercises": [
            {
                "number": "245",
                "instruction": "Спиши, вставляя буквы.",
                "text": "Наступила п_здняя ос_нь.",
            }
        ],
    },
    ensure_ascii=False,
)  # fmt: skip
NOTEBOOK = json.dumps({"role": "notebook", "exercises": []})


async def test_recognize_textbook_page() -> None:
    vision = FakeVision([TEXTBOOK])
    page, usage, _degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "textbook" and page.exercises[0].number == "245"
    assert (usage.calls, usage.tokens) == (1, 10)


async def test_notebook_page_has_no_text_from_vision() -> None:
    # модель нарушила промпт и всё же вернула рукопись — код обязан спрятать exercises сам
    handwritten = json.dumps(
        {"role": "notebook", "exercises": [{"number": "1", "text": "машына едет"}]},
        ensure_ascii=False,
    )
    vision = FakeVision([handwritten])
    page, _usage, _degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "notebook" and page.exercises == []


async def test_filled_in_workbook_is_not_a_reference() -> None:
    """Заполненная от руки рабочая тетрадь: vision прочитал бы вместе с рукописью ученика —
    эталон совпал бы с тем, что ребёнок написал, и проверка всегда говорила бы «верно». Такая
    страница не становится эталоном и не уходит в базу знаний (финальное ревью 17.09, I5)."""
    filled = json.dumps(
        {
            "role": "textbook",
            "handwritten": True,
            "exercises": [{"number": "245", "text": "Наступила поздняя осень."}],
        },
        ensure_ascii=False,
    )
    vision = FakeVision([filled])
    page, usage, _degrees = await recognize_page(vision, _jpeg(), model="v")
    assert (page.role, page.exercises) == ("unknown", [])
    assert page.comment == FILLED_BY_HAND_COMMENT
    assert usage.calls == 1  # доворачивать заполненную страницу незачем


async def test_unknown_role_tries_next_orientation_then_gives_up() -> None:
    unknown = json.dumps({"role": "unknown", "exercises": [], "comment": "пусто"})
    vision = FakeVision([unknown, unknown, unknown])
    page, usage, degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "unknown" and usage.calls == 3
    assert degrees == 0  # страницу не узнали ни в одной ориентации — поворачивать нечего


async def test_recognize_page_reports_the_orientation_it_recognised() -> None:
    """Ориентацию, в которой vision узнал страницу, возвращаем наверх: OCR обязан читать тот же
    кадр, иначе страница боком читается как мусор (живой прогон 18.09, ru-2: 184 слова)."""
    unknown = json.dumps({"role": "unknown", "exercises": []})
    vision = FakeVision([unknown, NOTEBOOK])
    page, _usage, degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "notebook" and degrees == ORIENTATIONS[1] == 270


async def test_invalid_json_counts_as_unknown() -> None:
    vision = FakeVision(["не json", NOTEBOOK])
    page, _usage, _degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "notebook"


async def test_textbook_without_exercises_tries_next_orientation() -> None:
    # страница перевёрнута — модель узнала печатный текст (role="textbook"), но не смогла
    # прочитать задания; следующая ориентация находит их
    empty_textbook = json.dumps({"role": "textbook", "exercises": []})
    vision = FakeVision([empty_textbook, TEXTBOOK])
    page, usage, _degrees = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "textbook" and page.exercises[0].number == "245"
    assert usage.calls == 2


def test_textbook_tasks_number_and_kind() -> None:
    page = RuPage(
        role="textbook",
        exercises=[
            RuExercise(number="245", text="Наступила п_здняя ос_нь."),
            RuExercise(number=None, text="(С)делать уроки."),
            RuExercise(number=None, text="Спиши текст."),
        ],
    )
    tasks = textbook_tasks(page, photo_path="2026-09-17/abc.jpg")
    assert [(t.number, t.number_on_page) for t in tasks] == [
        ("245", True),
        ("1", False),
        ("2", False),
    ]
    assert (
        tasks[0].condition == "Наступила п_здняя ос_нь."
        and tasks[0].photo_path == "2026-09-17/abc.jpg"
    )
    assert [task_kind(t.condition) for t in tasks] == ["fill_letters", "expand_brackets", "copy"]


def test_textbook_tasks_auto_number_avoids_printed_collision() -> None:
    page = RuPage(
        role="textbook",
        exercises=[
            RuExercise(number="2", text="Печатное упражнение."),
            RuExercise(number=None, text="Без номера."),
        ],
    )
    tasks = textbook_tasks(page, photo_path=None)
    assert [(t.number, t.number_on_page) for t in tasks] == [("2", True), ("1", False)]


def test_textbook_tasks_blank_first_exercise_does_not_consume_number_one() -> None:
    page = RuPage(
        role="textbook",
        exercises=[
            RuExercise(number=None, text="   "),
            RuExercise(number=None, text="Первое настоящее."),
            RuExercise(number=None, text="Второе настоящее."),
        ],
    )
    tasks = textbook_tasks(page, photo_path=None)
    assert [(t.number, t.number_on_page) for t in tasks] == [("1", False), ("2", False)]


def _word(text: str, line: int, x: int = 0) -> Word:
    return Word(text=text, box=Box(x0=x, y0=line * 20, x1=x + 30, y1=line * 20 + 15), line=line)


def test_notebook_task_number_from_header_and_lines() -> None:
    words = [
        _word("Упражнение", 0), _word("245.", 0, 40),
        _word("Наступила", 1), _word("позняя", 1, 40), _word("осень.", 1, 80),
    ]  # fmt: skip
    task = notebook_task(words)
    assert (task.number, task.number_on_page) == ("245", True)
    assert task.lines == ["Упражнение 245.", "Наступила позняя осень."]
    assert task.words == words


def test_notebook_task_without_number() -> None:
    task = notebook_task([_word("Наступила", 0), _word("осень", 0, 40)])
    assert (task.number, task.number_on_page) == ("1", False)


def test_notebook_task_orders_words_by_line_then_x() -> None:
    task = notebook_task([_word("осень", 0, 40), _word("Наступила", 0, 0)])
    assert task.lines == ["Наступила осень"]


def _boxed(text: str, top: int, x: int, *, height: int = 20, line: int | None = 0) -> Word:
    return Word(text=text, box=Box(x0=x, y0=top, x1=x + 30, y1=top + height), line=line)


def test_notebook_task_joins_engine_lines_that_are_one_row() -> None:
    """Порядок строк — свой, по рамкам (исследование 13.09). Движок на наклонной странице развёл
    одну строку на две и перепутал их местами: «Белка спрятала» оказалась ПОСЛЕ «орехи в»
    (живой прогон 18.09, ru-6)."""
    words = [
        _boxed("орехи", 630, 300, line=3),
        _boxed("в", 632, 400, line=3),
        _boxed("Белка", 628, 100, line=4),
        _boxed("спрятала", 626, 180, line=4),
        _boxed("дупло", 700, 100, line=5),
    ]
    task = notebook_task(words)
    assert task.lines == ["Белка спрятала орехи в", "дупло"]
    assert [(w.text, w.line) for w in task.words] == [
        ("Белка", 0), ("спрятала", 0), ("орехи", 0), ("в", 0), ("дупло", 1),
    ]  # fmt: skip


def test_notebook_task_keeps_a_slanted_row_together() -> None:
    """Строка «уезжает» вниз к правому краю: собранный движком кусок мы не рассыпаем — строки
    склеиваем, но не делим (на живых фото наклон доходит до высоты строки, ru-1 и ru-3 18.09)."""
    words = [
        _boxed("Я", 90, 0), _boxed("учусь", 94, 40), _boxed("писать", 98, 100),
        _boxed("ровно", 140, 0, line=1),
    ]  # fmt: skip
    task = notebook_task(words)
    assert task.lines == ["Я учусь писать", "ровно"]


def test_notebook_task_without_boxes_keeps_engine_lines() -> None:
    words = [Word(text="осень", line=1), Word(text="Наступила", line=0)]
    task = notebook_task(words)
    assert task.lines == ["Наступила", "осень"]


def test_notebook_task_keeps_words_without_a_line_out_of_rows() -> None:
    """Слово, которое движок ни в одну строку не собрал, — пометка на полях: своей геометрией мы
    его в строку тоже не вставляем (иначе цифра с поля вклинится в текст, ru-1 18.09)."""
    words = [
        _boxed("Наступила", 100, 0),
        _boxed("осень", 104, 100),
        _boxed("5", 102, 900, height=10, line=None),
    ]
    task = notebook_task(words)
    assert [w.line for w in task.words if w.text == "5"] == [None]
    assert "Наступила осень" in task.lines[0]


def test_header_number_ignores_dates_and_page_numbers() -> None:
    assert header_number(["17 сентября 2026"]) is None
    assert header_number(["стр. 12"]) is None
    assert header_number(["2026"]) is None
    assert header_number(["в 1945 году"]) is None


def test_header_number_bare_line() -> None:
    assert header_number(["245."]) == "245"


# --- переносы слов через дефис (живой прогон 18.09) ---


def _hyphen_word(text: str, line: int, x: int, confidence: float = 0.9) -> Word:
    return Word(
        text=text,
        box=Box(x0=x, y0=line * 20, x1=x + 30, y1=line * 20 + 15),
        confidence=confidence,
        line=line,
    )


def test_merge_hyphenation_joins_word_split_by_line_break() -> None:
    """«сред-» в конце строки и «них» в начале следующей — одно слово «средних»: без склейки
    обе половины уходили в находки (живой прогон 18.09, ru-1)."""
    words = [
        _hyphen_word("в", 0, 0),
        _hyphen_word("сред-", 0, 40, confidence=0.4),
        _hyphen_word("них", 1, 0, confidence=0.8),
        _hyphen_word("классах", 1, 40),
    ]
    merged = merge_hyphenation(words)
    assert [w.text for w in merged] == ["в", "средних", "классах"]
    joined = merged[1]
    assert joined.box is not None
    assert (joined.box.x0, joined.box.y0, joined.box.x1, joined.box.y1) == (0, 0, 70, 35)
    assert joined.confidence == 0.4 and joined.line == 0


def test_hyphen_not_at_the_end_of_a_line_is_not_a_hyphenation() -> None:
    words = [_hyphen_word("сред-", 0, 0), _hyphen_word("них", 0, 40)]
    assert [w.text for w in merge_hyphenation(words)] == ["сред-", "них"]


def test_single_letter_before_the_hyphen_is_a_dash_not_a_hyphenation() -> None:
    """«а -» в конце строки — тире, а не перенос: слово переносят минимум с двух букв."""
    words = [_hyphen_word("а-", 0, 0), _hyphen_word("Потом", 1, 0)]
    assert [w.text for w in merge_hyphenation(words)] == ["а-", "Потом"]


def test_dash_between_two_reference_words_is_not_merged() -> None:
    """«до дома — уставшие»: склеенного «домауставшие» в эталоне нет, а обе половины есть —
    это тире на конце строки, склеивать нельзя (живой прогон 18.09, ru-1)."""
    words = [_hyphen_word("дома-", 0, 0), _hyphen_word("уставшие", 1, 0)]
    reference = {"дома", "уставшие", "до"}
    assert [w.text for w in merge_hyphenation(words, reference)] == ["дома-", "уставшие"]
    # склеенное слово есть в эталоне — это перенос, а не тире
    assert [w.text for w in merge_hyphenation(words, {"домауставшие"})] == ["домауставшие"]


def test_merge_hyphenation_without_reference_prefers_merging() -> None:
    words = [_hyphen_word("круп-", 0, 0), _hyphen_word("ным", 1, 0)]
    assert [w.text for w in merge_hyphenation(words, set())] == ["крупным"]
