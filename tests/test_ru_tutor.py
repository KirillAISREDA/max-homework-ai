import json

from conftest import FakeLLMClient
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, WordTutoring, tutor_reply


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
