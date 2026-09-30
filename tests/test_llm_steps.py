"""Каждый шаг пайплайна объявляет себя в журнале вызовов: шаг = каталог промпта, версия промпта.

Клиент у шагов один и тот же, поэтому шаг и версию промпта знает только сам шаг — без этого
стоимость не разложить по шагам и не связать вызов с текстом промпта.
"""

import json
from pathlib import Path
from typing import Any

from conftest import FakeLLMClient
from hwcheck.events import EventLog
from hwcheck.llm.journal import JournaledLLM
from hwcheck.pipeline.classifier import classify_error
from hwcheck.pipeline.generator import generate_similar
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.praise import PraiseInput, generate_praise
from hwcheck.pipeline.solver import RefSolution, solve_task
from hwcheck.pipeline.tutor import tutor_reply
from hwcheck.pipeline.vision import recognize_page, recognize_page_two_stage
from hwcheck.prompts import PROMPTS_DIR
from hwcheck.subjects.russian.gaps import derive_text
from hwcheck.subjects.russian.recognize import recognize_page as recognize_ru_page
from hwcheck.subjects.russian.rules import classify_orthogram
from test_classifier_generator import BAD_EXERCISE, GOOD_EXERCISE, classifier_json
from test_praise import COLUMN, TECHNIQUE, WHY
from test_praise import reply as praise_json
from test_ru_gaps import WORDS
from test_ru_recognize import TEXTBOOK, FakeVision
from test_ru_tutor import _session as word_session
from test_tutor import make_session, turn
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, FakeTwoStageClient, make_image

REF = RefSolution(steps=["430/5 = 86"], answer="86", units="км/ч")
Step = tuple[str, str | None, str]


def journaled(inner: Any, tmp_path: Path) -> tuple[JournaledLLM, Path]:
    path = tmp_path / "events.jsonl"
    return JournaledLLM(inner, EventLog(path, "prod")), path


def steps(path: Path) -> list[Step]:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [(e["step"], e["prompt_version"], e["kind"]) for e in events if e["type"] == "llm_call"]


def assert_prompts_exist(found: list[Step]) -> None:
    """Пара «шаг, версия» из журнала должна находить файл промпта — иначе версия бесполезна."""
    for step, version, _kind in found:
        assert (PROMPTS_DIR / step / f"{version}.md").is_file(), (step, version)


async def test_two_stage_vision_is_two_steps_with_own_prompt_versions(tmp_path: Path) -> None:
    client, path = journaled(FakeTwoStageClient([TRANSCRIPT], [STRUCTURED]), tmp_path)
    await recognize_page_two_stage(
        client, make_image(), vision_model="GigaChat-2-Max", structure_model="GigaChat-2-Pro"
    )
    found = steps(path)
    assert found == [("vision", "v3", "vision"), ("vision_structure", "v1", "chat")]
    assert_prompts_exist(found)


async def test_two_stage_vision_every_orientation_is_a_call(tmp_path: Path) -> None:
    # нечитаемая страница: четыре ориентации — четыре оплаченных вызова, структуризации нет
    client, path = journaled(FakeTwoStageClient(["пусто"] * 4, []), tmp_path)
    await recognize_page_two_stage(client, make_image(), vision_model="v", structure_model="s")
    assert steps(path) == [("vision", "v3", "vision")] * 4


async def test_single_stage_vision_takes_version_from_caller(tmp_path: Path) -> None:
    client, path = journaled(FakeTwoStageClient([STRUCTURED, STRUCTURED], []), tmp_path)
    await recognize_page(client, make_image(), prompt="p", model="v", prompt_version="v2")
    await recognize_page(client, make_image(), prompt="p", model="v")
    assert steps(path) == [("vision", "v2", "vision"), ("vision", None, "vision")]


async def test_solver_step(tmp_path: Path) -> None:
    good = json.dumps({"steps": ["430/5 = 86"], "answer": "86", "units": None})
    client, path = journaled(FakeLLMClient([good]), tmp_path)
    await solve_task(client, "430 км за 5 ч", model="m")
    assert steps(path) == [("solver", "v1", "chat")]
    assert_prompts_exist(steps(path))


async def test_classifier_step(tmp_path: Path) -> None:
    client, path = journaled(FakeLLMClient([classifier_json(0.9)]), tmp_path)
    result = grade(["430:5=96"], "96", REF)
    await classify_error(client, "430 км за 5 ч", ["430:5=96"], "96", REF, result, model="m")
    assert steps(path) == [("classifier", "v1", "chat")]
    assert_prompts_exist(steps(path))


async def test_generator_step_counts_every_attempt(tmp_path: Path) -> None:
    client, path = journaled(FakeLLMClient([BAD_EXERCISE, GOOD_EXERCISE]), tmp_path)
    exercise = await generate_similar(client, "430 км за 5 ч, скорость?", None, model="m")
    assert exercise is not None
    assert steps(path) == [("generator", "v1", "chat")] * 2
    assert_prompts_exist(steps(path))


async def test_tutor_step_includes_leak_regeneration(tmp_path: Path) -> None:
    replies = [turn("Правильный ответ 9 3/4, проверь себя!"), turn("Сложи дроби сам")]
    client, path = journaled(FakeLLMClient(replies), tmp_path)
    reply, _ = await tutor_reply(client, make_session(), "не знаю", model="m")
    assert reply.startswith("Сложи дроби сам")  # вопрос дописывает код
    assert steps(path) == [("tutor", "v2", "chat")] * 2
    assert_prompts_exist(steps(path))


async def test_word_tutor_is_its_own_step(tmp_path: Path) -> None:
    replies = [
        json.dumps({"reply": "Правильно пишется «поздняя»!"}),
        json.dumps({"reply": "Подбери проверочное слово, где согласная слышится."}),
    ]
    client, path = journaled(FakeLLMClient(replies), tmp_path)
    await tutor_reply(client, word_session(), "не знаю", model="t")
    assert steps(path) == [("ru_tutor", "v1", "chat")] * 2
    assert_prompts_exist(steps(path))


async def test_ru_page_step(tmp_path: Path) -> None:
    client, path = journaled(FakeVision([TEXTBOOK]), tmp_path)
    await recognize_ru_page(client, make_image(), model="v")
    assert steps(path) == [("ru_page", "v1", "vision")]
    assert_prompts_exist(steps(path))


async def test_ru_gaps_step(tmp_path: Path) -> None:
    answer = json.dumps({"choices": [{"index": 3, "word": "леса"}]})
    client, path = journaled(FakeLLMClient([answer]), tmp_path)
    await derive_text("За дальние л_са несёт м_шина.", WORDS, client, model="m")
    assert steps(path) == [("ru_gaps", "v1", "chat")]
    assert_prompts_exist(steps(path))


async def test_ru_gaps_resolved_by_dictionary_makes_no_call(tmp_path: Path) -> None:
    client, path = journaled(FakeLLMClient([]), tmp_path)
    await derive_text("Наступила п_здняя осень.", WORDS, client, model="m")
    assert not path.exists()


async def test_ru_orthogram_step(tmp_path: Path) -> None:
    answer = json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.8})
    client, path = journaled(FakeLLMClient([answer]), tmp_path)
    await classify_orthogram(client, "машына", "машина", "Едет машына", model="s")
    assert steps(path) == [("ru_orthogram", "v1", "chat")]
    assert_prompts_exist(steps(path))


async def test_praise_step(tmp_path: Path) -> None:
    client, path = journaled(FakeLLMClient([praise_json((COLUMN.index, TECHNIQUE, WHY))]), tmp_path)
    await generate_praise(client, PraiseInput(tasks=[COLUMN]), model="m")
    assert steps(path) == [("praise", "v1", "chat")]
    assert_prompts_exist(steps(path))
