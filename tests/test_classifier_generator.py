import json

from conftest import FakeLLMClient
from hwcheck.pipeline.classifier import classify_error
from hwcheck.pipeline.generator import generate_similar
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.solver import RefSolution

REF = RefSolution(steps=["430/5 = 86"], answer="86", units="км/ч")


def classifier_json(confidence: float, error_type: str = "calc_error") -> str:
    return json.dumps(
        {
            "topic": "деление",
            "skill": "деление трёхзначного на однозначное",
            "error_type": error_type,
            "confidence": confidence,
            "explanation": "объяснение",
        },
        ensure_ascii=False,
    )


async def test_classify_confident() -> None:
    result = grade(["430:5=96"], "96", REF)
    client = FakeLLMClient([classifier_json(0.9)])
    analysis = await classify_error(
        client, "430 км за 5 ч", ["430:5=96"], "96", REF, result, model="m"
    )
    assert analysis.error_type == "calc_error"
    # данные валидатора включены в промпт классификатора
    assert "96" in "\n".join(m.content for m in client.calls[0])


async def test_low_confidence_becomes_unclear() -> None:
    result = grade(["430:5=96"], "96", REF)
    client = FakeLLMClient([classifier_json(0.4)])
    analysis = await classify_error(
        client, "430 км за 5 ч", ["430:5=96"], "96", REF, result, model="m"
    )
    assert analysis.error_type == "unclear"  # fail-safe арх. §3.3


GOOD_EXERCISE = json.dumps(
    {
        "task_text": "Поезд проехал 360 км за 4 часа. Найди скорость.",
        "ref_steps": ["360/4 = 90"],
        "ref_answer": "90",
        "units": "км/ч",
    },
    ensure_ascii=False,
)
BAD_EXERCISE = json.dumps(
    {
        "task_text": "Поезд проехал 360 км за 4 часа. Найди скорость.",
        "ref_steps": ["360/4 = 80"],
        "ref_answer": "80",
        "units": "км/ч",
    },
    ensure_ascii=False,
)


async def test_generator_valid_exercise() -> None:
    client = FakeLLMClient([GOOD_EXERCISE])
    exercise = await generate_similar(client, "430 км за 5 ч, скорость?", None, model="m")
    assert exercise is not None
    assert exercise.ref_answer == "90"


async def test_generator_retries_once_then_gives_up() -> None:
    # эталон с арифметической ошибкой оба раза → None (арх. §3.3: задача отбрасывается)
    client = FakeLLMClient([BAD_EXERCISE, BAD_EXERCISE])
    exercise = await generate_similar(client, "задача", None, model="m")
    assert exercise is None
    assert len(client.calls) == 2


async def test_generator_retry_succeeds() -> None:
    client = FakeLLMClient([BAD_EXERCISE, GOOD_EXERCISE])
    exercise = await generate_similar(client, "задача", None, model="m")
    assert exercise is not None
    assert exercise.ref_answer == "90"
