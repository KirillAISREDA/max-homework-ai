"""Русский: роль страницы и печатный текст — vision; тетрадь — слова OCR → задание."""

import json
from collections.abc import Sequence

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.subjects.base import Box, Word
from hwcheck.subjects.russian.recognize import (
    RuExercise,
    RuPage,
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)


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
    page, usage = await recognize_page(vision, b"img", model="v")
    assert page.role == "textbook" and page.exercises[0].number == "245"
    assert (usage.calls, usage.tokens) == (1, 10)


async def test_notebook_page_has_no_text_from_vision() -> None:
    vision = FakeVision([NOTEBOOK])
    page, _ = await recognize_page(vision, b"img", model="v")
    assert page.role == "notebook" and page.exercises == []


async def test_unknown_role_tries_next_orientation_then_gives_up() -> None:
    unknown = json.dumps({"role": "unknown", "exercises": [], "comment": "пусто"})
    vision = FakeVision([unknown, unknown, unknown])
    page, usage = await recognize_page(vision, b"img", model="v")
    assert page.role == "unknown" and usage.calls == 3


async def test_invalid_json_counts_as_unknown() -> None:
    vision = FakeVision(["не json", NOTEBOOK])
    page, _ = await recognize_page(vision, b"img", model="v")
    assert page.role == "notebook"


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
        ("2", False),
        ("3", False),
    ]
    assert (
        tasks[0].condition == "Наступила п_здняя ос_нь."
        and tasks[0].photo_path == "2026-09-17/abc.jpg"
    )
    assert [task_kind(t.condition) for t in tasks] == ["fill_letters", "expand_brackets", "copy"]


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
