"""Шаг «похвала»: модель только формулирует, что ученик сделал правильно; верность решения уже
установил валидатор. Всё, что модель могла выдумать, отсекает детерминированная проверка."""

import json

import pytest

from conftest import FakeLLMClient
from hwcheck.bot.check import validator_only_grade
from hwcheck.llm.base import StructuredOutputError
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.praise import (
    MAX_PRAISE_CHARS,
    PraiseInput,
    PraiseItem,
    PraiseOutput,
    PraiseTask,
    accepted_praise,
    describable,
    fallback_praise,
    generate_praise,
)
from hwcheck.pipeline.solver import RefSolution
from hwcheck.prompts import load_prompt

COLUMN = PraiseTask(
    index=0, condition="Вычисли: 803 + 169", steps=["803 + 169 = 972"], answer="972"
)
WHY = "Ты правильно сложил единицы и не забыл перенести десяток: 803 + 169 = 972."
TECHNIQUE = "сложение столбиком с переходом через десяток"


def output(*items: tuple[int, str, str]) -> PraiseOutput:
    return PraiseOutput(
        items=[PraiseItem(index=i, technique=technique, why=why) for i, technique, why in items]
    )


def reply(*items: tuple[int, str, str]) -> str:
    return json.dumps(output(*items).model_dump(), ensure_ascii=False)


def accepted(why: str, technique: str = TECHNIQUE, task: PraiseTask = COLUMN) -> dict[int, str]:
    return accepted_praise(PraiseInput(tasks=[task]), output((task.index, technique, why)))


# --- вызов модели ---


async def test_prompt_carries_tasks_and_grade() -> None:
    client = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    data = PraiseInput(tasks=[COLUMN], grade=3)

    result, llm_result = await generate_praise(client, data, model="m")

    assert result.items[0].why == WHY
    assert llm_result.tokens_in + llm_result.tokens_out == 15
    [system, user] = client.calls[0]
    assert system.role == "system" and system.content == load_prompt("praise", "v1")
    assert "803 + 169 = 972" in user.content
    assert "Вычисли: 803 + 169" in user.content
    assert "Класс ученика: 3" in user.content


async def test_prompt_without_grade_does_not_invent_it() -> None:
    client = FakeLLMClient([reply((0, TECHNIQUE, WHY))])
    await generate_praise(client, PraiseInput(tasks=[COLUMN]), model="m")
    assert "Класс ученика" not in client.calls[0][1].content


async def test_invalid_json_raises_after_one_retry() -> None:
    client = FakeLLMClient(["не json", "снова не json"])
    with pytest.raises(StructuredOutputError):
        await generate_praise(client, PraiseInput(tasks=[COLUMN]), model="m")
    assert len(client.calls) == 2


# --- детерминированная защита ---


def test_good_praise_is_composed_for_the_child() -> None:
    assert accepted(WHY) == {0: f"{WHY} Приём — {TECHNIQUE}."}


def test_whitespace_and_trailing_dots_are_normalized() -> None:
    texts = accepted("Ты правильно\nсложил  единицы", technique=" Сложение столбиком. ")
    assert texts == {0: "Ты правильно сложил единицы. Приём — сложение столбиком."}


def test_number_outside_the_solution_is_rejected() -> None:
    # 973 в решении нет: модель «пересчитала» сама — такой текст ребёнку не показываем
    assert accepted("Ты правильно сложил числа: 803 + 169 = 973.") == {}
    assert accepted("Ты верно сложил числа.", technique="сложение в 2 действия") == {}


def test_numbers_are_compared_by_value_with_this_task_only() -> None:
    fractions = PraiseTask(index=4, steps=["7 1/8 + 2 5/8 = 9 6/8"], answer="9 3/4")
    assert 4 in accepted("Ты верно сложил целые части и дроби: 7 1/8 и 2 5/8.", task=fractions)
    decimal = PraiseTask(index=5, steps=["4,7 + 0,3 = 5"])
    assert 5 in accepted("Ты аккуратно сложил десятичные дроби 4,7 и 0,3.", task=decimal)
    # число другого (тоже верного) задания этому заданию чужое
    data = PraiseInput(tasks=[COLUMN, decimal])
    mixed_up = output((5, TECHNIQUE, "Ты правильно сложил 803 и 169."))
    assert accepted_praise(data, mixed_up) == {}


@pytest.mark.parametrize(
    "why",
    [
        "Ты сложил числа, но забыл про десяток.",
        "Ты сложил числа, однако можно было короче.",
        "Здесь была ошибка в единицах.",
        "Ты ошибся в одном месте.",
        "Ответ неверный в середине.",
        "Ты сложил неправильно, потом поправил.",
        "Тебе стоит исправить запись.",
        "Хотя запись неаккуратная, всё сложено.",
    ],
)
def test_error_markers_are_rejected(why: str) -> None:
    assert accepted(why) == {}


def test_words_ending_with_no_are_not_error_markers() -> None:
    # «правильно», «верно», «точно» кончаются на «но» — это не союз
    assert 0 in accepted("Ты правильно и точно сложил единицы, всё верно записал.")


def test_too_long_praise_is_rejected() -> None:
    long_why = "Ты правильно сложил единицы " + "и десятки " * 30 + "."
    assert len(long_why) > MAX_PRAISE_CHARS
    assert accepted(long_why) == {}


def test_empty_fields_are_rejected() -> None:
    assert accepted("   ") == {}
    assert accepted(WHY, technique="") == {}


def test_foreign_index_is_ignored() -> None:
    data = PraiseInput(tasks=[COLUMN])
    assert accepted_praise(data, output((7, TECHNIQUE, "Ты правильно сложил числа."))) == {}


def test_first_item_for_an_index_wins() -> None:
    data = PraiseInput(tasks=[COLUMN])
    doubled = output((0, TECHNIQUE, WHY), (0, TECHNIQUE, "Ты правильно сложил десятки."))
    assert accepted_praise(data, doubled) == {0: f"{WHY} Приём — {TECHNIQUE}."}


# --- запасной текст из данных валидатора ---


def test_fallback_counts_verified_actions() -> None:
    one = validator_only_grade(["803 + 169 = 972"])
    assert fallback_praise(one) == "Пересчитал твоё действие — сходится."
    three = validator_only_grade(["2 + 2 = 4", "3 + 3 = 6", "4 + 4 = 8"])
    assert fallback_praise(three) == "Пересчитал 3 действия — все сходятся."
    five = validator_only_grade([f"{n} + 1 = {n + 1}" for n in range(5)])
    assert fallback_praise(five) == "Пересчитал 5 действий — все сходятся."


def test_fallback_mentions_answer_matched_with_reference() -> None:
    ref = RefSolution(steps=["220 + 180 = 400", "700 - 400 = 300"], answer="300")
    solved = grade(["220 + 180 = 400", "700 - 400 = 300"], "300", ref)
    assert fallback_praise(solved) == "Пересчитал 2 действия — все сходятся, и ответ верный."
    answer_only = grade([], "300", ref)
    assert fallback_praise(answer_only) == "Ответ сошёлся с моим пересчётом."


def test_fallback_for_slip_does_not_claim_all_steps_match() -> None:
    ref = RefSolution(steps=["700 - 400 = 300"], answer="300")
    slip = grade(["220 + 180 = 410", "700 - 400 = 300"], "300", ref)
    assert slip.verdict == "correct" and slip.slip_lines == [1]
    assert fallback_praise(slip) == "Ответ сошёлся с моим пересчётом."


# --- что отдаём модели ---


def test_describable_needs_clean_steps() -> None:
    ref = RefSolution(steps=["700 - 400 = 300"], answer="300")
    assert describable(validator_only_grade(["803 + 169 = 972"]), ["803 + 169 = 972"])
    # шагов нет — описывать нечего
    assert not describable(grade([], "300", ref), [])
    # описка в шаге при верном ответе: модель повторила бы неверную строку
    slip_steps = ["220 + 180 = 410", "700 - 400 = 300"]
    assert not describable(grade(slip_steps, "300", ref), slip_steps)
