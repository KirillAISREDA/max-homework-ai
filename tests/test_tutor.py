import json

from conftest import FakeLLMClient
from hwcheck.pipeline.classifier import ErrorAnalysis
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, tutor_reply

REF = RefSolution(steps=["7 1/8 + 2 5/8 = 9 6/8", "9 6/8 = 9 3/4"], answer="9 3/4", units=None)
ERROR = ErrorAnalysis(
    topic="дроби",
    skill="сложение дробей с одинаковым знаменателем",
    error_type="calc_error",
    confidence=0.9,
    explanation="Ошибка при сложении числителей",
)


def make_session() -> TutorSession:
    return TutorSession(
        task_text="Вычисли: 7 1/8 + 2 5/8",
        student_steps=["7 1/8 + 2 5/8 = 9 5/8"],
        student_answer="9 5/8",
        ref=REF,
        error=ERROR,
        first_error_line=1,
    )


def prompt_text(client: FakeLLMClient, call_index: int = 0) -> str:
    return "\n".join(m.content for m in client.calls[call_index])


def turn(reply: str) -> str:
    return json.dumps({"reply": reply}, ensure_ascii=False)


async def test_hint_level_grows_by_code_not_llm() -> None:
    session = make_session()
    for expected_level in (1, 2, 3, 3):  # растёт на 1 за реплику, потолок 3
        client = FakeLLMClient([turn("реплика тьютора")])
        _, session = await tutor_reply(client, session, "не знаю", model="m")
        assert session.hint_level == expected_level


async def test_answer_not_in_prompt_below_level_3() -> None:
    session = make_session()
    # уровни 1 и 2: эталонного ответа и шагов нет в промпте
    for _ in range(2):
        client = FakeLLMClient([turn("реплика")])
        _, session = await tutor_reply(client, session, "не знаю", model="m")
        assert "9 3/4" not in prompt_text(client)
        assert "9 6/8" not in prompt_text(client)
    # уровень 3: эталон в промпте
    client = FakeLLMClient([turn("показываю решение")])
    _, session = await tutor_reply(client, session, "не знаю", model="m")
    assert session.hint_level == 3
    assert "9 3/4" in prompt_text(client)


async def test_error_line_only_from_level_1() -> None:
    session = make_session()
    client = FakeLLMClient([turn("реплика")])
    _, session = await tutor_reply(client, session, "помоги", model="m")
    # уровень 1: указываем шаг с ошибкой, но не значения
    assert "шаге 1" in prompt_text(client) or "шаг 1" in prompt_text(client)


async def test_correct_answer_resolves_session() -> None:
    session = make_session()
    client = FakeLLMClient([turn("молодец!")])
    reply, session = await tutor_reply(client, session, "9 3/4", model="m")
    assert session.resolved is True
    assert session.hint_level == 0  # уровень не вырос
    assert reply == "молодец!"


async def test_correct_answer_with_units_resolves() -> None:
    session = make_session()
    client = FakeLLMClient([turn("верно")])
    _, session = await tutor_reply(client, session, "получилось 9 6/8", model="m")
    # 9 6/8 == 9 3/4 математически
    assert session.resolved is True


async def test_leaked_answer_triggers_regeneration() -> None:
    # модель решила задачу сама и назвала ответ на уровне 1 — фильтр ловит,
    # перегенерация возвращает чистую реплику
    session = make_session()
    client = FakeLLMClient([turn("Правильный ответ 9 3/4, проверь себя!"), turn("Сложи дроби сам")])
    reply, session = await tutor_reply(client, session, "скажи ответ", model="m")
    assert reply == "Сложи дроби сам"
    assert len(client.calls) == 2
    # в retry-запросе есть стоп-инструкция
    assert "СТОП" in prompt_text(client, 1)


async def test_persistent_leak_replaced_with_safe_redirect() -> None:
    from hwcheck.pipeline.tutor import SAFE_REDIRECT

    session = make_session()
    client = FakeLLMClient([turn("Ответ: 9 3/4"), turn("Всё равно скажу: 39/4!")])
    reply, _ = await tutor_reply(client, session, "ну скажи", model="m")
    assert reply == SAFE_REDIRECT  # 39/4 == 9 3/4 математически — тоже утечка


async def test_numbers_from_task_are_not_blocked() -> None:
    # числа из условия и решения ученика ребёнок и так видит — не блокируем
    session = make_session()
    client = FakeLLMClient([turn("Посмотри ещё раз: сколько будет 1/8 + 5/8?")])
    reply, _ = await tutor_reply(client, session, "не знаю", model="m")
    assert "1/8" in reply
    assert len(client.calls) == 1


async def test_level_3_reveal_not_blocked() -> None:
    session = make_session().model_copy(update={"hint_level": 2})
    client = FakeLLMClient([turn("Смотри: 1/8+5/8=6/8, итого 9 6/8 = 9 3/4")])
    reply, session = await tutor_reply(client, session, "сдаюсь", model="m")
    assert session.hint_level == 3
    assert "9 3/4" in reply  # на уровне 3 показывать решение можно


async def test_structured_output_error_degrades_gracefully() -> None:
    from hwcheck.pipeline.tutor import SAFE_RETRY

    session = make_session()
    client = FakeLLMClient(["не json", "снова не json"])  # оба вызова невалидны
    reply, session = await tutor_reply(client, session, "не знаю", model="m")
    assert reply == SAFE_RETRY
    assert session.hint_level == 1  # состояние FSM сохранилось


async def test_history_accumulates() -> None:
    session = make_session()
    client = FakeLLMClient([turn("реплика 1")])
    _, session = await tutor_reply(client, session, "не знаю", model="m")
    client2 = FakeLLMClient([turn("реплика 2")])
    _, session = await tutor_reply(client2, session, "всё ещё не знаю", model="m")
    contents = [m.content for m in session.history]
    assert contents == ["не знаю", "реплика 1", "всё ещё не знаю", "реплика 2"]
    # история диалога попадает в следующий промпт
    assert "реплика 1" in prompt_text(client2)
