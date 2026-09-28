"""Абстракции LLM/Vision (арх. §4: «Абстракция провайдера»).

Все шаги пайплайна зависят только от этих протоколов; GigaChat — реализация №1.
"""

import json
import re
from collections.abc import Sequence
from typing import Any, Literal, Protocol

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
    """Модель не вернула валидный JSON по схеме даже после retry.

    result — LLMResult последней попытки (с накопленными токенами обоих вызовов),
    чтобы вызывающий мог учесть расход даже при неудаче.
    """

    def __init__(self, message: str, result: "LLMResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> str:
    match = _JSON_FENCE.search(text)
    if match:
        return match.group(1)
    return text.strip()


_CLOSERS = {"{": "}", "[": "]"}


def close_brackets(text: str) -> str:
    """Ответ с потерянными закрывающими скобками в конце — дописанный; иначе текст как был.

    GigaChat теряет последнюю «}» у полного по смыслу ответа. Дописываются только скобки,
    открытые вне строк; оборванная строка, лишняя или чужая скобка — не этот случай.
    """
    opened: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in _CLOSERS:
            opened.append(char)
        elif char in _CLOSERS.values() and (not opened or _CLOSERS[opened.pop()] != char):
            return text
    if in_string or not opened:
        return text
    return text + "".join(_CLOSERS[char] for char in reversed(opened))


class _Glued(list[dict[str, Any]]):
    """Объекты, склеенные в один: ключи повторяются, на повторе начинается следующий объект."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any] | _Glued:
    parts: list[dict[str, Any]] = [{}]
    for key, value in pairs:
        if key in parts[-1]:
            parts.append({})
        parts[-1][key] = value
    if len(parts) == 1:
        return parts[0]
    if any(part.keys() != parts[0].keys() for part in parts):
        # наборы полей разные — модель ещё и переставила или пропустила поля: где граница
        # объектов, неизвестно, и расклейка приписала бы значение чужому объекту (ревью 28.09).
        # Остаётся обычное правило JSON, как до починки
        return dict(pairs)
    return _Glued(parts)


def _unglue(value: Any) -> tuple[Any, bool]:
    """Значение без склеек и признак, что они были. В списке склейка — несколько объектов;
    вне списка делить не на что, остаётся правило JSON «последний ключ побеждает»."""
    if isinstance(value, _Glued):
        merged: dict[str, Any] = {}
        for part in value:
            merged |= part
        return _unglue(merged)[0], True
    if isinstance(value, dict):
        items = {key: _unglue(item) for key, item in value.items()}
        return {k: v for k, (v, _) in items.items()}, any(found for _, found in items.values())
    if isinstance(value, list):
        result: list[Any] = []
        found = False
        for item in value:
            parts = list(item) if isinstance(item, _Glued) else [item]
            found = found or isinstance(item, _Glued)
            for part in parts:
                clean, inner = _unglue(part)
                result.append(clean)
                found = found or inner
        return result, found
    return value, False


def repair_json(raw: str) -> str:
    """Ответ модели с исправленными сбоями записи GigaChat; целый JSON не меняется.

    Сбоев два (живая проба 28.09): потеряны закрывающие скобки в конце и потеряно «}, {» между
    объектами списка. Второй обычный разбор не замечает: ключи повторяются, побеждает последний,
    и от списка остаётся один объект. Расклеиваются только объекты с одинаковым набором полей.
    """
    closed = close_brackets(raw)
    try:
        value, glued = _unglue(json.loads(closed, object_pairs_hook=_object))
    except (ValueError, RecursionError):
        # не JSON или бесконечная вложенность (модель зациклилась): решает проверка по схеме
        return closed
    return json.dumps(value, ensure_ascii=False) if glued else closed


def _parse[T: BaseModel](schema: type[T], content: str) -> T:
    """Ответ по схеме; сбой записи, который чинится без модели, повторного вызова не стоит."""
    return schema.model_validate_json(repair_json(extract_json(content)))


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
        return _parse(schema, result.content), result
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
            return _parse(schema, retry_result.content), retry_result
        except ValidationError as retry_error:
            raise StructuredOutputError(
                f"Невалидный JSON после retry (model={model}, schema={schema.__name__})",
                retry_result,
            ) from retry_error
