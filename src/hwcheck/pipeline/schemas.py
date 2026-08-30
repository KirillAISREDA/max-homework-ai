"""Контракты шагов пайплайна (арх. §3.3): каждый шаг — (input: dict) -> dict по схеме."""

from pydantic import BaseModel, Field


class VisionTask(BaseModel):
    number: int = Field(description="Номер задания на странице")
    task_text: str = Field(description="Условие задания как напечатано/написано")
    student_solution_steps: list[str] = Field(
        default_factory=list, description="Шаги решения ученика, по строкам"
    )
    student_answer: str | None = Field(default=None, description="Итоговый ответ ученика")
    confidence: float = Field(ge=0.0, le=1.0, description="Уверенность распознавания задания")


class VisionPage(BaseModel):
    """Результат Vision/Parser: сегментация фото на задания.

    Арх. §3.3: confidence < 0.7 по заданию → просить переснять/подтвердить.
    """

    tasks: list[VisionTask]
    page_ok: bool = Field(description="Фото пригодно: не обрезано, читаемо, это математика")
    page_comment: str | None = Field(default=None, description="Почему фото непригодно, если так")
