import json
from pathlib import Path

from conftest import FakeLLMClient
from hwcheck.pipeline.solver import FileCache, solve_task

GOOD = json.dumps({"steps": ["430/5 = 86"], "answer": "86", "units": "км/ч"}, ensure_ascii=False)
BAD_MATH = json.dumps({"steps": ["430/5 = 96"], "answer": "96", "units": None}, ensure_ascii=False)


async def test_solve_and_self_check() -> None:
    client = FakeLLMClient([GOOD])
    solved, llm_result = await solve_task(client, "430 км за 5 ч, скорость?", model="m")
    assert solved.ref_ok is True
    assert solved.solution.answer == "86"
    assert llm_result is not None
    # системный промпт солвера подставлен
    assert client.calls[0][0].role == "system"


async def test_invalid_ref_marked_not_ok() -> None:
    client = FakeLLMClient([BAD_MATH])
    solved, _ = await solve_task(client, "задание", model="m")
    assert solved.ref_ok is False


async def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = FileCache(tmp_path)
    client = FakeLLMClient([GOOD])
    first, _ = await solve_task(client, "задание", model="m", cache=cache)
    assert first.from_cache is False

    second, llm_result = await solve_task(FakeLLMClient([]), "задание", model="m", cache=cache)
    assert second.from_cache is True
    assert llm_result is None
    assert second.solution == first.solution


async def test_invalid_ref_not_cached(tmp_path: Path) -> None:
    cache = FileCache(tmp_path)
    await solve_task(FakeLLMClient([BAD_MATH]), "задание", model="m", cache=cache)
    # следующий вызов не должен найти кэш — уходит в LLM снова
    solved, llm_result = await solve_task(FakeLLMClient([GOOD]), "задание", model="m", cache=cache)
    assert solved.ref_ok is True
    assert llm_result is not None
