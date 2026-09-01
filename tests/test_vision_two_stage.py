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
