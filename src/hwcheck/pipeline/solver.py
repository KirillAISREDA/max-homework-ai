"""Solver (арх. §3.3): независимое эталонное решение задания.

Эталон сразу проверяется Validator'ом (self-check). Эталон с арифметической
ошибкой помечается ref_ok=False — использовать его для вердикта нельзя,
такие случаи уходят в эскалацию.
"""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from hwcheck.llm.base import (
    ChatMessage,
    LLMClient,
    LLMResult,
    StructuredOutputError,
    chat_structured,
)
from hwcheck.pipeline.validator import check_steps
from hwcheck.prompts import load_prompt


class RefSolution(BaseModel):
    steps: list[str] = Field(description="Шаги-вычисления в линейной нотации, по строке")
    answer: str = Field(description="Итоговый ответ: число или выражение, без единиц")
    units: str | None = Field(default=None, description="Единицы измерения, если есть")


class SolvedTask(BaseModel):
    solution: RefSolution
    ref_ok: bool  # эталон прошёл самопроверку Validator'ом
    from_cache: bool
    model: str
    prompt_version: str


class FileCache:
    """Кэш решений для CLI/офлайн-этапа (арх. §4: в проде — Redis, TTL 30 дней)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, key: str) -> dict[str, object] | None:
        path = self._root / f"{key}.json"
        try:
            data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            # битый файл (прерванная запись) — считаем промахом и убираем
            path.unlink(missing_ok=True)
            return None
        return data

    def put(self, key: str, value: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def cache_key(task_text: str, *, model: str, prompt_version: str) -> str:
    raw = f"{model}\n{prompt_version}\n{task_text.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def solve_task(
    client: LLMClient,
    task_text: str,
    *,
    model: str,
    prompt_version: str = "v1",
    cache: FileCache | None = None,
) -> tuple[SolvedTask, LLMResult | None]:
    """Возвращает (решение, LLMResult | None если из кэша).

    StructuredOutputError пробрасывается наверх — вызывающий решает про эскалацию.
    """
    key = cache_key(task_text, model=model, prompt_version=prompt_version)
    if cache is not None and (cached := cache.get(key)) is not None:
        solution = RefSolution.model_validate(cached)
        return _solved(solution, model=model, prompt_version=prompt_version, from_cache=True), None

    system_prompt = load_prompt("solver", prompt_version)
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_text),
    ]
    solution, llm_result = await chat_structured(client, messages, RefSolution, model=model)
    solved = _solved(solution, model=model, prompt_version=prompt_version, from_cache=False)
    if cache is not None and solved.ref_ok:  # невалидный эталон не кэшируем
        cache.put(key, solution.model_dump())
    return solved, llm_result


def _solved(
    solution: RefSolution, *, model: str, prompt_version: str, from_cache: bool
) -> SolvedTask:
    checks = check_steps(solution.steps)
    ref_ok = all(c.status != "mismatch" for c in checks) and bool(solution.answer.strip())
    return SolvedTask(
        solution=solution,
        ref_ok=ref_ok,
        from_cache=from_cache,
        model=model,
        prompt_version=prompt_version,
    )


__all__ = [
    "FileCache",
    "RefSolution",
    "SolvedTask",
    "StructuredOutputError",
    "cache_key",
    "solve_task",
]
