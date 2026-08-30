import io
import json

from PIL import Image

from hwcheck.llm.base import LLMResult
from hwcheck.pipeline.vision import ORIENTATIONS, recognize_page

EMPTY_PAGE = json.dumps(
    {"tasks": [], "page_ok": False, "page_comment": "Неразборчивый почерк"}, ensure_ascii=False
)
GOOD_PAGE = json.dumps(
    {
        "tasks": [
            {
                "number": 1,
                "task_text": "2+2",
                "student_solution_steps": ["2+2=4"],
                "student_answer": "4",
                "confidence": 0.95,
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


class FakeVisionClient:
    """Отдаёт заданные ответы по очереди и записывает размеры присланных картинок."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.image_sizes: list[tuple[int, int]] = []
        self.filenames: list[str] = []

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.image_sizes.append(Image.open(io.BytesIO(image)).size)
        self.filenames.append(filename)
        return LLMResult(
            content=self._responses.pop(0), model=model, tokens_in=100, tokens_out=50, latency_s=1.0
        )


async def test_first_orientation_succeeds() -> None:
    client = FakeVisionClient([GOOD_PAGE])
    rec = await recognize_page(client, make_image(), prompt="p", model="m")
    assert rec.page is not None and len(rec.page.tasks) == 1
    assert (rec.orientation, rec.attempts) == (0, 1)
    assert client.image_sizes == [(1280, 960)]
    # байты после нормализации всегда JPEG — имя должно соответствовать
    assert client.filenames == ["page.jpg"]


async def test_rotation_ladder_finds_tasks() -> None:
    client = FakeVisionClient([EMPTY_PAGE, GOOD_PAGE])
    rec = await recognize_page(client, make_image(), prompt="p", model="m")
    assert rec.page is not None and len(rec.page.tasks) == 1
    assert (rec.orientation, rec.attempts) == (270, 2)
    # вторая попытка — фото повёрнуто, размеры поменялись местами
    assert client.image_sizes == [(1280, 960), (960, 1280)]
    # токены и латентность суммируются по всем попыткам
    assert rec.tokens_in == 200
    assert rec.latency_s == 2.0


async def test_all_orientations_empty_returns_first_valid() -> None:
    client = FakeVisionClient([EMPTY_PAGE] * len(ORIENTATIONS))
    rec = await recognize_page(client, make_image(), prompt="p", model="m")
    assert rec.page is not None and rec.page.tasks == []
    assert rec.attempts == len(ORIENTATIONS)
    assert rec.orientation == 0


async def test_invalid_json_everywhere_returns_none() -> None:
    client = FakeVisionClient(["не json"] * len(ORIENTATIONS))
    rec = await recognize_page(client, make_image(), prompt="p", model="m")
    assert rec.page is None
    assert rec.raw == "не json"
    assert rec.attempts == len(ORIENTATIONS)
