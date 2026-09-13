"""Задание из нескольких пунктов (живые логи 06.09: эталон «80» на 4 выражения,
тьютор 7 реплик на уровне 3 так и не засчитал ответ).

Проверка многопунктового задания — по арифметике каждой строки, а цель разбора —
верное значение ошибочной строки, посчитанное SymPy, а не общий ответ солвера.
"""

import json

from conftest import FakeLLMClient
from hwcheck.bot.fsm import CheckedTask
from hwcheck.bot.handlers import Bot, _validator_only_grade
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, tutor_reply

CONDITION = "Вычисли: а) 40 + 35; б) 90 - 18; в) 12 * 3; г) 80 : 4"
LUMPED_REF = RefSolution(steps=["80 : 4 = 20"], answer="80")  # солвер свёл пункты в одно
STEPS = ["а) 40 + 35 = 75", "б) 90 - 18 = 72", "в) 12 * 3 = 36", "г) 80 : 4 = 20"]


def turn(reply: str) -> str:
    return json.dumps({"reply": reply}, ensure_ascii=False)


def test_multipart_task_graded_by_lines_not_lumped_answer() -> None:
    result = grade(STEPS, "20", LUMPED_REF, condition=CONDITION)
    assert result.verdict == "correct"


def test_multipart_task_with_wrong_line_is_wrong_at_that_line() -> None:
    steps = [*STEPS[:1], "б) 90 - 18 = 82", *STEPS[2:]]
    result = grade(steps, None, LUMPED_REF, condition=CONDITION)
    assert (result.verdict, result.first_error_line) == ("wrong", 2)


def test_multipart_detected_by_student_item_markers() -> None:
    assert grade(STEPS, None, LUMPED_REF, condition="Вычисли").verdict == "correct"


def test_single_task_still_compares_with_reference_answer() -> None:
    ref = RefSolution(steps=["40 + 35 = 75"], answer="75")
    assert grade(["40 + 35 = 75"], "76", ref, condition="Вычисли 40 + 35").verdict == "wrong"


def make_session(**update: object) -> TutorSession:
    session = TutorSession(
        task_text=CONDITION,
        student_steps=[*STEPS[:1], "б) 90 - 18 = 82", *STEPS[2:]],
        student_answer=None,
        ref=LUMPED_REF,
        first_error_line=2,
        expected="72",
    )
    return session.model_copy(update=update)


async def test_correct_value_of_error_line_resolves_session() -> None:
    client = FakeLLMClient([turn("молодец!")])
    _, session = await tutor_reply(client, make_session(), "72", model="m")
    assert session.resolved


async def test_corrected_line_as_equality_resolves_session() -> None:
    client = FakeLLMClient([turn("молодец!")])
    _, session = await tutor_reply(client, make_session(), "90 - 18 = 72", model="m")
    assert session.resolved


async def test_expected_value_is_guarded_before_level_3() -> None:
    client = FakeLLMClient([turn("Получится 72"), turn("Пересчитай вычитание ещё раз")])
    reply, _ = await tutor_reply(client, make_session(), "не знаю", model="m")
    assert "72" not in reply


async def test_level_3_context_names_error_line_value() -> None:
    client = FakeLLMClient([turn("разбираем")])
    _, _ = await tutor_reply(client, make_session(hint_level=2), "не знаю", model="m")
    prompt = "\n".join(m.content for m in client.calls[0])
    assert "72" in prompt


async def test_bot_sets_tutor_target_from_error_line() -> None:
    bot = Bot(None, None, None, None, None)  # type: ignore[arg-type]
    steps = ["803 + 169 = 972", "972 - 100 = 862"]
    item = CheckedTask(
        task=VisionTask(number=1, task_text="", student_solution_steps=steps, confidence=1),
        ref=None,
        grade=_validator_only_grade(steps),
    )
    session = await bot._start_tutoring(None, item)
    assert session.expected == "872"
