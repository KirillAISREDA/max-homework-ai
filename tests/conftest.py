from collections.abc import Sequence

from pydantic import BaseModel

from hwcheck.llm.base import ChatMessage, LLMResult


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
