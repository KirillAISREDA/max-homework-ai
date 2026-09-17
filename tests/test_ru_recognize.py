"""Русский: роль страницы и печатный текст — vision; тетрадь — слова OCR → задание."""

import io
import json
from collections.abc import Sequence

from PIL import Image

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.subjects.base import Box, Word
from hwcheck.subjects.russian.recognize import (
    FILLED_BY_HAND_COMMENT,
    RuExercise,
    RuPage,
    header_number,
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
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
    page, usage = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "textbook" and page.exercises[0].number == "245"
    assert (usage.calls, usage.tokens) == (1, 10)


async def test_notebook_page_has_no_text_from_vision() -> None:
    # модель нарушила промпт и всё же вернула рукопись — код обязан спрятать exercises сам
    handwritten = json.dumps(
        {"role": "notebook", "exercises": [{"number": "1", "text": "машына едет"}]},
        ensure_ascii=False,
    )
    vision = FakeVision([handwritten])
    page, _ = await recognize_page(vision, _jpeg(), model="v")
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
    page, usage = await recognize_page(vision, _jpeg(), model="v")
    assert (page.role, page.exercises) == ("unknown", [])
    assert page.comment == FILLED_BY_HAND_COMMENT
    assert usage.calls == 1  # доворачивать заполненную страницу незачем


async def test_unknown_role_tries_next_orientation_then_gives_up() -> None:
    unknown = json.dumps({"role": "unknown", "exercises": [], "comment": "пусто"})
    vision = FakeVision([unknown, unknown, unknown])
    page, usage = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "unknown" and usage.calls == 3


async def test_invalid_json_counts_as_unknown() -> None:
    vision = FakeVision(["не json", NOTEBOOK])
    page, _ = await recognize_page(vision, _jpeg(), model="v")
    assert page.role == "notebook"


async def test_textbook_without_exercises_tries_next_orientation() -> None:
    # страница перевёрнута — модель узнала печатный текст (role="textbook"), но не смогла
    # прочитать задания; следующая ориентация находит их
    empty_textbook = json.dumps({"role": "textbook", "exercises": []})
    vision = FakeVision([empty_textbook, TEXTBOOK])
    page, usage = await recognize_page(vision, _jpeg(), model="v")
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


def test_header_number_ignores_dates_and_page_numbers() -> None:
    assert header_number(["17 сентября 2026"]) is None
    assert header_number(["стр. 12"]) is None
    assert header_number(["2026"]) is None
    assert header_number(["в 1945 году"]) is None


def test_header_number_bare_line() -> None:
    assert header_number(["245."]) == "245"
