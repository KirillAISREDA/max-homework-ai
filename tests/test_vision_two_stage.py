import io
import json

from PIL import Image

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.pipeline.vision import looks_like_math, recognize_page_two_stage

TRANSCRIPT = "стр. 5\n№ 4\n999 + 1 = 1000\n900 - 1 = 899"
GIBBERISH = "тут ничего не видно"
STRUCTURED = json.dumps(
    {
        "tasks": [
            {
                "number": 4,
                "task_text": "",
                "student_solution_steps": ["999 + 1 = 1000", "900 - 1 = 899"],
                "student_answer": None,
                "confidence": 0.9,
            }
        ],
        "page_ok": True,
        "page_comment": None,
    },
    ensure_ascii=False,
)


def make_image() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 960), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeTwoStageClient:
    """Vision-транскрипции и текстовые вызовы по очереди из своих списков."""

    def __init__(self, transcripts: list[str], chats: list[str]) -> None:
        self._transcripts = list(transcripts)
        self._chats = list(chats)
        self.vision_calls = 0
        self.chat_calls = 0

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.vision_calls += 1
        return LLMResult(
            content=self._transcripts.pop(0),
            model=model,
            tokens_in=9000,
            tokens_out=150,
            latency_s=2.0,
        )

    async def chat(
        self, messages: list[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        self.chat_calls += 1
        return LLMResult(
            content=self._chats.pop(0), model=model, tokens_in=500, tokens_out=200, latency_s=1.0
        )


def test_looks_like_math() -> None:
    assert looks_like_math(TRANSCRIPT) is True
    assert looks_like_math(GIBBERISH) is False
    assert looks_like_math("") is False
    # цифры без формы вычисления (заголовки) — не математика
    assert looks_like_math("стр. 5 № 4 Домашняя работа 2026") is False
    # короткий пример младших классов — математика
    assert looks_like_math("2+2=4") is True


def structured_page(confidences: list[float], n_tasks: int | None = None) -> str:
    tasks = [
        {
            "number": i + 1,
            "task_text": "",
            "student_solution_steps": [f"{i}+1={i + 1}"],
            "student_answer": None,
            "confidence": c,
        }
        for i, c in enumerate(confidences)
    ]
    return json.dumps(
        {"tasks": tasks, "page_ok": bool(tasks), "page_comment": None if tasks else "нет заданий"},
        ensure_ascii=False,
    )


async def test_low_confidence_continues_ladder() -> None:
    # мусор не той ориентации структурировался с низкой уверенностью —
    # лестница продолжается и находит уверенный вариант
    client = FakeTwoStageClient(
        [TRANSCRIPT, TRANSCRIPT], [structured_page([0.2]), structured_page([0.9])]
    )
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is not None
    assert rec.orientation == 270
    assert rec.page.tasks[0].confidence == 0.9


async def test_all_empty_preserves_page_comment() -> None:
    # структуризация везде вернула 0 заданий — сохраняем её диагноз, а не None
    client = FakeTwoStageClient([TRANSCRIPT] * 4, [structured_page([])] * 4)
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is not None
    assert rec.page.tasks == []
    assert rec.page.page_comment == "нет заданий"


async def test_structured_error_tokens_counted() -> None:
    # обе попытки структуризации невалидны → расход всё равно учитывается
    client = FakeTwoStageClient([TRANSCRIPT] + [GIBBERISH] * 3, ["не json", "снова не json"])
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is None
    # 4 vision по 9000 + 2 chat по 500 (retry внутри chat_structured)
    assert rec.tokens_in == 4 * 9000 + 2 * 500


async def test_first_orientation_success() -> None:
    client = FakeTwoStageClient([TRANSCRIPT], [STRUCTURED])
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is not None and len(rec.page.tasks) == 1
    assert rec.page.tasks[0].student_solution_steps[0] == "999 + 1 = 1000"
    assert (rec.orientation, rec.attempts) == (0, 1)
    assert (client.vision_calls, client.chat_calls) == (1, 1)


async def test_gibberish_transcript_rotates_without_structuring() -> None:
    # мусорная транскрипция не тратит вызов структуризации — сразу поворот
    client = FakeTwoStageClient([GIBBERISH, TRANSCRIPT], [STRUCTURED])
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is not None and rec.page.tasks
    assert rec.orientation == 270
    assert (client.vision_calls, client.chat_calls) == (2, 1)


async def test_all_orientations_gibberish_returns_none() -> None:
    client = FakeTwoStageClient([GIBBERISH] * 4, [])
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.page is None
    assert client.chat_calls == 0
    assert rec.raw == GIBBERISH  # транскрипция сохраняется для диагностики


async def test_tokens_accumulate_across_stages() -> None:
    client = FakeTwoStageClient([TRANSCRIPT], [STRUCTURED])
    rec = await recognize_page_two_stage(
        client, make_image(), vision_model="vm", structure_model="sm"
    )
    assert rec.tokens_in == 9500  # vision + структуризация
    assert rec.latency_s == 3.0
