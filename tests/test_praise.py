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


TWO_STEPS = PraiseTask(
    index=1, condition="Вычисли: 56 : 7 + 12", steps=["56 : 7 = 8", "8 + 12 = 20"], answer="20"
)
TWO_STEPS_WHY = "Сначала ты правильно разделил 56 на 7, получив 8, затем добавил 12."


def test_count_of_actions_is_checked_against_the_record() -> None:
    """«В два действия» — не выдуманное число, а счёт строк решения (живая проба 28.09)."""
    for technique in ("деление и сложение в два действия", "решение в 2 действия"):
        text = accepted(TWO_STEPS_WHY, technique=technique, task=TWO_STEPS)[1]
        assert text.endswith(f"Приём — {technique}.")


def test_wrong_name_of_technique_leaves_the_explanation() -> None:
    """Название приёма не прошло проверку — объяснение остаётся: оно ближе к решению ребёнка,
    чем запасной текст."""
    # действий два, а не три; в решении COLUMN действие одно
    assert accepted(TWO_STEPS_WHY, technique="решение в три действия", task=TWO_STEPS) == {
        1: TWO_STEPS_WHY
    }
    assert accepted("Ты верно сложил числа.", technique="сложение в 2 действия") == {
        0: "Ты верно сложил числа."
    }


def test_unsafe_explanation_is_not_rescued_by_technique() -> None:
    assert accepted("Ты нашёл 1 % и умножил: 803 + 169 = 972.") == {}
    assert accepted("Ты решил верно, но медленно.") == {}


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
        # ревью: раздельное написание — та же мысль, что «неверно»
        "Способ записан не верно, а итог сошёлся.",
        "Ты записал не правильно, а сложил хорошо.",
        "Но сложил ты хорошо.",
    ],
)
def test_error_markers_are_rejected(why: str) -> None:
    assert accepted(why) == {}


@pytest.mark.parametrize(
    "why",
    [
        "Ты быстро получил пятьсот, всё сложено.",
        "У тебя вышло девятьсот семьдесят три, всё сложено.",
        "Ты сложил числа в четыре действия.",
        "Ты получил две тысячи, всё сложено.",
    ],
)
def test_number_written_in_words_is_checked_like_digits(why: str) -> None:
    """Ревью: текст без единой цифры проходил проверку чисел целиком — «пятьсот» то же число."""
    assert accepted(why) == {}


def test_known_number_in_words_and_place_value_words_pass() -> None:
    # 972 есть в решении; «десяток», «единицы», «сотни» — разряды, не числа
    assert 0 in accepted("Ты получил девятьсот семьдесят два, сложив единицы, десятки и сотни.")
    assert 0 in accepted("Ты сначала сложил единицы, потом перенёс десяток.")
    small = PraiseTask(index=0, steps=["2 + 3 = 5"])
    assert 0 in accepted("Ты верно сложил два и три.", task=small)


def test_thousands_written_with_a_space_are_one_number() -> None:
    """Ревью: «1 000» читалось как два числа, и верная похвала уходила в запасной текст."""
    shared = PraiseTask(index=0, condition="Всего 1000 рублей", steps=["1000 : 4 = 250"])
    assert 0 in accepted("Ты верно разделил 1 000 на 4 и получил 250.", task=shared)
    written = PraiseTask(index=0, steps=["12 500 + 500 = 13 000"])
    assert 0 in accepted("Ты верно сложил 12500 и 500, получилось 13 000.", task=written)
    assert accepted("Ты верно разделил 2 000 на 4.", task=shared) == {}


def test_links_are_not_shown_to_the_child() -> None:
    # запись ребёнка уходит в промпт как есть: текст из неё не должен вернуться ссылкой
    assert accepted("Ты верно сложил числа, подробнее на https://example.com.") == {}
    assert accepted("Ты верно сложил числа, пиши на www.example.com.") == {}


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
