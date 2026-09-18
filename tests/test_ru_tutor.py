import json

import pytest

from conftest import FakeLLMClient
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import (
    WORD_REDIRECT,
    TutorSession,
    WordTutoring,
    tutor_reply,
)


def _session() -> TutorSession:
    return TutorSession(
        task_text="Слово «позняя» в предложении: «Наступила позняя осень».",
        student_steps=[],
        student_answer="позняя",
        ref=RefSolution(steps=[], answer="поздняя", units=None),
        expected="поздняя",
        word=WordTutoring(
            actual="позняя",
            expected="поздняя",
            sentence="Наступила позняя осень",
            rule_code="ru.orth.silent_consonant",
            rule_title="Непроизносимая согласная",
            rule_statement="Подбери слово, где согласная слышится: опоздать.",
            rule_example="поздно — опоздать",
        ),  # fmt: skip
    )


async def test_word_tutor_uses_ru_prompt_and_levels() -> None:
    llm = FakeLLMClient([json.dumps({"reply": "Какая орфограмма спряталась в этом слове?"})])
    reply, session = await tutor_reply(llm, _session(), "Помоги найти ошибку", model="t")
    assert session.hint_level == 1 and not session.resolved
    system = llm.calls[0][0].content
    assert "орфограмм" in system.lower()  # prompts/ru_tutor/v1.md, не математический
    assert "Непроизносимая согласная" in llm.calls[0][1].content


async def test_word_tutor_hides_expected_word_before_level_3() -> None:
    llm = FakeLLMClient(
        [
            json.dumps({"reply": "Правильно пишется «поздняя»!"}),
            json.dumps({"reply": "Подбери проверочное слово, где согласная слышится."}),
        ]
    )
    reply, _ = await tutor_reply(llm, _session(), "не знаю", model="t")
    assert "поздняя" not in reply.lower() and "проверочное" in reply


async def test_word_tutor_resolves_on_expected_word() -> None:
    llm = FakeLLMClient([json.dumps({"reply": "Верно! Опоздать — поздняя."})])
    reply, session = await tutor_reply(llm, _session(), "поздняя", model="t")
    assert session.resolved and session.hint_level == 0


async def test_word_tutor_level_3_reveals_word() -> None:
    session = _session().model_copy(update={"hint_level": 2})
    llm = FakeLLMClient([json.dumps({"reply": "Пишется «поздняя»: проверочное слово — опоздать."})])
    reply, session = await tutor_reply(llm, session, "всё равно не понимаю", model="t")
    assert session.hint_level == 3 and "поздняя" in reply


async def test_word_tutor_passes_prompt_version_through() -> None:
    # prompt_version не должен быть захардкожен на "v1" в _word_reply — версия промпта
    # пишется в БД рядом с результатом (арх. §4), путь должен реально уйти в load_prompt
    llm = FakeLLMClient([json.dumps({"reply": "неважно"})])
    with pytest.raises(FileNotFoundError):
        await tutor_reply(llm, _session(), "не знаю", model="t", prompt_version="v2")


async def test_word_tutor_does_not_put_the_answer_in_the_prompt_before_level_3() -> None:
    """До уровня 3 верного написания нет и в контексте: модуль обещает, что ответ попадает в
    промпт только на уровне 3, а «НЕ называй «поздняя»» — это и есть ответ (ревью 17.09, I9)."""
    llm = FakeLLMClient([json.dumps({"reply": "Какое правило здесь работает?"})])
    await tutor_reply(llm, _session(), "не знаю", model="t")
    context = llm.calls[0][1].content
    assert "поздняя" not in context.lower()
    assert "НЕ называй верное написание слова" in context


async def test_word_tutor_blocks_the_letter_before_level_3() -> None:
    """Реплика без самого слова, но с нужной буквой («пиши через «д»») — та же подсказка ответа."""
    llm = FakeLLMClient(
        [
            json.dumps({"reply": "Пиши через «д» — так правильно."}),
            json.dumps({"reply": "Здесь нужна буква д."}),
        ]
    )
    reply, _ = await tutor_reply(llm, _session(), "не знаю", model="t")
    assert reply == WORD_REDIRECT


async def test_word_tutor_lets_the_rule_through_before_level_3() -> None:
    llm = FakeLLMClient([json.dumps({"reply": "Подбери слово, где согласная слышится: опоздать."})])
    reply, _ = await tutor_reply(llm, _session(), "не знаю", model="t")
    assert reply == "Подбери слово, где согласная слышится: опоздать."


async def test_word_tutor_level_3_may_name_the_letter() -> None:
    session = _session().model_copy(update={"hint_level": 2})
    llm = FakeLLMClient([json.dumps({"reply": "Здесь нужна буква «д»: опоздать — поздняя."})])
    reply, session = await tutor_reply(llm, session, "не понимаю", model="t")
    assert session.hint_level == 3 and "«д»" in reply
