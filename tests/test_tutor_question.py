"""Разбор ошибки: вопрос задаёт код (спецификация 2026-09-30-tutor-code-question-design.md).

Живой разбор 30.09 (№2.181): модель спросила про вычитание в пункте а), ребёнок дважды верно ответил
«36» и дважды услышал «пересчитай», а «Молодец!» модель сказала сама — код разбор не закрыл.
"""

import json

import pytest

from conftest import FakeLLMClient
from hwcheck.pipeline.mathparse import action_values
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import SAFE_PRAISE_REDIRECT, TutorSession, tutor_reply

CONDITION = "Выполните действия: а) 39 452 - 16 452 : (300 - 264); б) 93 601 - 601 * (231 - 88)"
LINE_A = "а) 39452 - 16452 : (300 - 264) = 38997"


def turn(reply: str) -> str:
    return json.dumps({"reply": reply}, ensure_ascii=False)


def live_session(**update: object) -> TutorSession:
    session = TutorSession(
        task_text=CONDITION,
        student_steps=[LINE_A, "б) 93601 - 601 * (231 - 88) = 7658"],
        student_answer=None,
        ref=RefSolution(steps=[], answer="38995"),
        first_error_line=1,
        expected="38995",
        target_line=LINE_A,
    )
    return session.model_copy(update=update)


def prompt_text(client: FakeLLMClient, call_index: int = 0) -> str:
    return "\n".join(m.content for m in client.calls[call_index])


# --- значения действий ---


def test_action_values_follow_school_order() -> None:
    values = {str(v) for v in action_values("39452 - 16452 : (300 - 264)")}
    assert {"36", "457", "38995"} <= values
    assert "300" not in values  # число из записи — не действие


def test_action_values_include_partial_sums_of_several_terms() -> None:
    values = {str(v) for v in action_values("а) 329503 + 12146 * 28 + 715 * 449")}
    assert {"340088", "321035", "669591", "990626"} <= values


@pytest.mark.parametrize("text", ["", "abc", "2 +", "9**9**9", "x + 1"])
def test_action_values_of_garbage_are_empty(text: str) -> None:
    assert action_values(text) == []


# --- вопрос от кода ---


@pytest.mark.parametrize(
    ("target_line", "question"),
    [
        (LINE_A, "Сколько получается в пункте а)? Напиши ответ числом."),
        ("700 - 400 = 310", "Сколько будет 700 − 400? Напиши ответ числом."),
        ("x = 12 - 5 = 8", "Чему равно x? Напиши ответ числом."),
        (None, "Какой ответ получается в задаче? Напиши его числом."),
    ],
)
async def test_code_appends_its_question_to_the_hint(
    target_line: str | None, question: str
) -> None:
    client = FakeLLMClient([turn("Посмотри, какое действие выполняется первым.")])
    reply, _ = await tutor_reply(
        client, live_session(target_line=target_line), "Помоги найти ошибку", model="m"
    )
    assert reply == f"Посмотри, какое действие выполняется первым.\n{question}"


async def test_context_names_the_error_line_by_its_text() -> None:
    client = FakeLLMClient([turn("Подсказка")])
    await tutor_reply(client, live_session(), "Помоги найти ошибку", model="m")
    assert f"шаге 1: «{LINE_A}»" in prompt_text(client)


async def test_solved_reply_has_no_question() -> None:
    client = FakeLLMClient([turn("Молодец! Порядок действий соблюдён.")])
    reply, session = await tutor_reply(client, live_session(hint_level=1), "38995", model="m")
    assert session.resolved and reply == "Молодец! Порядок действий соблюдён."


# --- промежуточные значения ---


async def test_right_intermediate_value_does_not_raise_hint_level() -> None:
    client = FakeLLMClient([turn("Верно, 36! Теперь раздели 16452 на это число.")])
    reply, session = await tutor_reply(client, live_session(hint_level=1), "36", model="m")
    assert (session.hint_level, session.resolved) == (1, False)
    assert session.known_values == ["36"]
    assert "верно посчитал действие: 36" in prompt_text(client)
    assert len(client.calls) == 1  # «36» названо ребёнком — не утечка, «Верно» — не лишняя похвала
    assert reply.startswith("Верно, 36!") and reply.endswith("Напиши ответ числом.")


async def test_unnamed_action_value_is_a_secret_before_level_3() -> None:
    client = FakeLLMClient([turn("Сначала 300 − 264 = 36."), turn("Начни с действия в скобках.")])
    reply, _ = await tutor_reply(client, live_session(), "не знаю", model="m")
    assert reply.startswith("Начни с действия в скобках.")
    assert "СТОП" in prompt_text(client, 1)


async def test_live_dialog_of_30_09_closes_by_code() -> None:
    session = live_session()
    replies = ["Посмотри на порядок действий.", "Верно!", "Верно, это первое действие.", "Молодец!"]
    for message, reply in zip(["Помоги найти ошибку", "36", "36", "38995"], replies, strict=True):
        _, session = await tutor_reply(FakeLLMClient([turn(reply)]), session, message, model="m")
    assert session.resolved and session.hint_level == 1


# --- похвала ---


async def test_praise_without_accepted_answer_is_regenerated() -> None:
    client = FakeLLMClient(
        [turn("Молодец! Теперь всё правильно."), turn("Проверь, правильно ли ты разделил.")]
    )
    reply, session = await tutor_reply(client, live_session(hint_level=1), "38000", model="m")
    assert not session.resolved
    assert reply.startswith("Проверь, правильно ли ты разделил.")
    assert "СТОП" in prompt_text(client, 1)


async def test_persistent_praise_is_replaced() -> None:
    client = FakeLLMClient([turn("Отлично!"), turn("Верно, молодец!")])
    reply, _ = await tutor_reply(client, live_session(hint_level=2), "38000", model="m")
    assert reply.startswith(SAFE_PRAISE_REDIRECT)


async def test_wrong_answer_context_forbids_praise() -> None:
    client = FakeLLMClient([turn("Посмотри на деление.")])
    await tutor_reply(client, live_session(hint_level=1), "38000", model="m")
    assert "не хвали" in prompt_text(client)


# --- выход после уровня 3 ---


async def test_wrong_answer_after_shown_solution_closes_without_model() -> None:
    client = FakeLLMClient([])  # модель не зовём
    reply, session = await tutor_reply(client, live_session(hint_level=3), "38990", model="m")
    assert session.closed and not session.resolved
    assert "38995" in reply and "Запиши" in reply
    assert client.calls == []


async def test_reaching_level_3_shows_solution_and_keeps_dialog_open() -> None:
    client = FakeLLMClient([turn("Смотри: 300 − 264 = 36, 16452 : 36 = 457, 39452 − 457 = 38995.")])
    reply, session = await tutor_reply(client, live_session(hint_level=2), "38990", model="m")
    assert (session.hint_level, session.closed) == (3, False)
    assert "38995" in reply
