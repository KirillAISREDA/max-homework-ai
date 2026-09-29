import asyncio
from collections.abc import Iterator, Sequence

import pytest
from pydantic import BaseModel

from hwcheck.bot.models import MaxUpdate
from hwcheck.events import set_id_hash_key
from hwcheck.llm.base import ChatMessage, LLMResult


@pytest.fixture(autouse=True)
def _reset_id_hash_key() -> Iterator[None]:
    # ключ HMAC — глобальное состояние процесса: тест не должен влиять на соседей
    yield
    set_id_hash_key(None)


class FakeLLMClient:
    """LLMClient, отдающий заранее заданные ответы и записывающий вызовы."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult:
        self.calls.append(list(messages))
        return LLMResult(
            content=self._responses.pop(0),
            model=model,
            tokens_in=10,
            tokens_out=5,
            latency_s=0.1,
        )


class Answer(BaseModel):
    value: int
    comment: str


# --- параллельная обработка апдейтов (bot/dispatch.py, bot/runner.py) ---


def chat_update(chat_id: int | None, name: str, *, user_id: int = 42) -> MaxUpdate:
    """Текстовое сообщение чата; `name` — текст, по нему фейковый бот узнаёт апдейт.
    Без чата — служебный апдейт (у таких MAX не присылает ни сообщения, ни `chat_id`)."""
    if chat_id is None:
        return MaxUpdate.model_validate({"update_type": name})
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
                "body": {"mid": name, "text": name, "attachments": []},
            },
        }
    )


async def settle(rounds: int = 20) -> None:
    """Даёт задачам цикла событий дойти до ближайшего ожидания — без настоящих пауз."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class GatedBot:
    """Фейковый бот с управляемыми задержками: апдейт с «воротами» ждёт, пока тест их откроет."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str] = []
        self.failing: set[str] = set()  # эти апдейты падают с исключением
        self.self_cancelling: set[str] = set()  # эти — отменяются изнутри (не по остановке)
        self.running = 0
        self.peak = 0  # больше всего одновременных обработок
        self._gates: dict[str, asyncio.Event] = {}
        self._started: dict[str, asyncio.Event] = {}
        self._finished: dict[str, asyncio.Event] = {}

    def hold(self, *names: str) -> None:
        for name in names:
            self._gates[name] = asyncio.Event()

    def release(self, *names: str) -> None:
        for name in names:
            self._gates[name].set()

    # срок — страховка от зависшего теста, а не ожидание: в норме событие приходит сразу
    async def wait_started(self, name: str, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._started.setdefault(name, asyncio.Event()).wait(), timeout)

    async def wait_finished(self, name: str, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._finished.setdefault(name, asyncio.Event()).wait(), timeout)

    async def handle_update(self, update: MaxUpdate) -> None:
        body = update.message.body if update.message is not None else None
        name = (body.text if body is not None else None) or update.update_type
        self.started.append(name)
        self._started.setdefault(name, asyncio.Event()).set()
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0)  # и быстрый апдейт отдаёт управление, как настоящий
            if name in self._gates:
                await self._gates[name].wait()
            if name in self.failing:
                raise RuntimeError(f"сбой обработки {name}")
            if name in self.self_cancelling:
                raise asyncio.CancelledError
            self.finished.append(name)
            self._finished.setdefault(name, asyncio.Event()).set()
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise
        finally:
            self.running -= 1


class ScriptedPoller:
    """GET /updates по сценарию: батчи по очереди, после них — простой long poll до отмены."""

    def __init__(self, *batches: list[MaxUpdate]) -> None:
        self._batches = list(batches)
        self.calls = 0
        self.idle = asyncio.Event()  # сценарий кончился: раннер висит в простое

    async def get_updates(
        self, marker: int | None, *, timeout: int = 30
    ) -> tuple[list[MaxUpdate], int | None]:
        self.calls += 1
        if self._batches:
            return self._batches.pop(0), self.calls
        self.idle.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
