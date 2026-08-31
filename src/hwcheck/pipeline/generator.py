"""Exercise Generator (арх. §3.3): похожая задача для закрепления навыка.

Эталон сгенерированной задачи проверяется Validator'ом; задача без валидного
эталона отбрасывается (один retry, затем None — вызывающий берёт задачу из пула).
"""

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.pipeline.classifier import ErrorAnalysis
from hwcheck.pipeline.validator import check_steps
from hwcheck.prompts import load_prompt

MAX_ATTEMPTS = 2


class GeneratedExercise(BaseModel):
    task_text: str = Field(description="Условие новой задачи")
    ref_steps: list[str] = Field(description="Эталонные шаги в линейной нотации")
    ref_answer: str = Field(description="Эталонный ответ без единиц")
    units: str | None = None


async def generate_similar(
    client: LLMClient,
    task_text: str,
    error: ErrorAnalysis | None,
    *,
    model: str,
    prompt_version: str = "v1",
) -> GeneratedExercise | None:
    system_prompt = load_prompt("generator", prompt_version)
    skill_hint = (
        f"Задача должна тренировать навык: {error.skill} (тема: {error.topic})."
        if error is not None
        else "Задача должна тренировать тот же навык, что и исходная."
    )
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(
            role="user",
            content=f"Исходная задача: {task_text}\n{skill_hint}",
        ),
    ]
    for _ in range(MAX_ATTEMPTS):
        try:
            exercise, _ = await chat_structured(client, messages, GeneratedExercise, model=model)
        except StructuredOutputError:
            continue
        if _valid(exercise):
            return exercise
    return None


def _valid(exercise: GeneratedExercise) -> bool:
    if not exercise.ref_steps or not exercise.ref_answer.strip():
        return False
    checks = check_steps(exercise.ref_steps)
    return all(c.status != "mismatch" for c in checks)
