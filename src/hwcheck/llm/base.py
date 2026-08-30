"""Абстракции LLM/Vision (арх. §4: «Абстракция провайдера»).

Все шаги пайплайна зависят только от этих протоколов; GigaChat — реализация №1.
"""

import re
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class LLMResult(BaseModel):
    content: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float = 0.0


class LLMClient(Protocol):
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult: ...


class VisionClient(Protocol):
    async def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        model: str,
        filename: str = "image.jpg",
    ) -> LLMResult: ...


class StructuredOutputError(RuntimeError):
    """Модель не вернула валидный JSON по схеме даже после retry."""


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> str:
    match = _JSON_FENCE.search(text)
    if match:
        return match.group(1)
    return text.strip()


async def chat_structured[T: BaseModel](
    client: LLMClient,
    messages: Sequence[ChatMessage],
    schema: type[T],
    *,
    model: str,
    temperature: float = 0.1,
) -> tuple[T, LLMResult]:
    """Вызов с валидацией ответа по pydantic-схеме.

    Арх. §4: при невалидном JSON — ровно один retry с указанием на ошибку,
    затем StructuredOutputError (fallback решает вызывающий шаг).
    """
    result = await client.chat(messages, model=model, temperature=temperature)
    try:
        return schema.model_validate_json(extract_json(result.content)), result
    except ValidationError as first_error:
        retry_messages = [
            *messages,
            ChatMessage(role="assistant", content=result.content),
            ChatMessage(
                role="user",
                content=(
                    "Твой ответ не прошёл валидацию по JSON-схеме: "
                    f"{first_error.errors(include_url=False)!r}. "
                    "Верни только исправленный JSON, без пояснений и без markdown."
                ),
            ),
        ]
        retry_result = await client.chat(retry_messages, model=model, temperature=temperature)
        retry_result.tokens_in += result.tokens_in
        retry_result.tokens_out += result.tokens_out
        retry_result.latency_s += result.latency_s
        try:
            return schema.model_validate_json(extract_json(retry_result.content)), retry_result
        except ValidationError as retry_error:
            raise StructuredOutputError(
                f"Невалидный JSON после retry (model={model}, schema={schema.__name__})"
            ) from retry_error
