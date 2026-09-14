from collections.abc import Iterator, Sequence

import pytest
from pydantic import BaseModel

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
