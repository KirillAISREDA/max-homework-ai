"""Comparator + Error Classifier (арх. §3.3): тип ошибки по таксономии.

LLM получает факты от Validator'а (номер шага, посчитанные значения) и только
классифицирует. Fail-safe: confidence < 0.6 → error_type = unclear, тьютор
задаст уточняющий вопрос вместо уверенного разбора не той ошибки.
"""

from typing import Literal

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, chat_structured
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.solver import RefSolution
from hwcheck.prompts import load_prompt

MIN_CONFIDENCE = 0.6

ErrorType = Literal[
    "calc_error",  # арифметическая ошибка
    "strategy_error",  # неверный способ решения
    "condition_misread",  # неверно понято условие
    "slip",  # описка при верном понимании
    "incomplete",  # решение не доведено до конца
    "unclear",  # не удалось однозначно определить
]


class ErrorAnalysis(BaseModel):
    topic: str = Field(description="Тема, например «дроби»")
    skill: str = Field(description="Конкретный навык, например «сложение дробей»")
    error_type: ErrorType
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str = Field(description="Краткое объяснение сути ошибки, 1-2 предложения")


async def classify_error(
    client: LLMClient,
    task_text: str,
    student_steps: list[str],
    student_answer: str | None,
    ref: RefSolution,
    grade_result: GradeResult,
    *,
    model: str,
    prompt_version: str = "v1",
) -> ErrorAnalysis:
    system_prompt = load_prompt("classifier", prompt_version)
    facts = _facts(task_text, student_steps, student_answer, ref, grade_result)
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=facts),
    ]
    analysis, _ = await chat_structured(client, messages, ErrorAnalysis, model=model)
    if analysis.confidence < MIN_CONFIDENCE:
        analysis = analysis.model_copy(update={"error_type": "unclear"})
    return analysis


def _facts(
    task_text: str,
    student_steps: list[str],
    student_answer: str | None,
    ref: RefSolution,
    grade_result: GradeResult,
) -> str:
    validator_lines = []
    for i, check in enumerate(grade_result.line_checks, start=1):
        if check.status == "mismatch":
            validator_lines.append(
                f"шаг {i}: расхождение, вычисленные значения сегментов: {check.values}"
            )
    steps = "\n".join(student_steps) or "(шаги не распознаны)"
    return (
        f"Задание: {task_text}\n\n"
        f"Решение ученика:\n{steps}\n"
        f"Ответ ученика: {student_answer or '(нет)'}\n\n"
        f"Эталонное решение:\n" + "\n".join(ref.steps) + f"\n"
        f"Эталонный ответ: {ref.answer}\n\n"
        f"Проверка арифметики (детерминированная):\n"
        + ("\n".join(validator_lines) or "арифметических расхождений в шагах нет")
        + f"\nИтоговый вердикт: {grade_result.verdict}"
    )
