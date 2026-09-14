"""Проверка альбома без мессенджера: распознавание страниц, разбор на учебник и тетрадь,
проверка заданий.

Один код для бота (`handlers.py` добавляет MAX, состояние и журнал событий) и для стенда
сравнения моделей (`hwcheck.bench`): стенд меряет ровно то, что работает в проде.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from hwcheck.bot.pages import (
    PageRole,
    mark_written_numbers,
    merge_textbook,
    page_role,
    split_columns,
)
from hwcheck.llm.base import LLMResult
from hwcheck.pipeline.grade import GradeResult, grade, grade_by_lines
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import (
    FileCache,
    RefSolution,
    SolvedTask,
    StructuredOutputError,
    solve_task,
)
from hwcheck.pipeline.validator import check_steps
from hwcheck.pipeline.vision import RecognizedPage, VisionAndChatClient, recognize_page_two_stage

logger = logging.getLogger(__name__)

RefStatus = Literal["no_condition", "solver_failed", "ref_not_verified", "ok"]


@dataclass(frozen=True)
class CheckModels:
    vision: str  # транскрипция фото
    structure: str  # разбор транскрипции на задания
    solver: str  # эталонное решение


@dataclass
class RecognizedPhoto:
    page: VisionPage | None
    role: PageRole
    rec: RecognizedPage  # токены, попытки, сырая транскрипция


@dataclass
class AlbumPages:
    notebook: list[VisionTask]  # задания тетради — проверяются
    textbook: list[VisionTask]  # все известные условия (прошлые + из этого альбома)
    new_textbook: list[VisionTask]  # условия со страниц учебника этого альбома
    comment: str | None  # диагноз модели по непригодной странице


@dataclass
class TaskCheck:
    task: VisionTask
    ref: RefSolution | None
    grade: GradeResult
    ref_status: RefStatus
    solved: SolvedTask | None  # None — условия нет или солвер упал
    solver_result: LLMResult | None  # None — из кэша или солвер не вызывался


async def recognize_photo(
    llm: VisionAndChatClient, image: bytes, models: CheckModels
) -> RecognizedPhoto:
    rec = await recognize_page_two_stage(
        llm, image, vision_model=models.vision, structure_model=models.structure
    )
    page = mark_written_numbers(_split_task_columns(rec.page), rec.raw) if rec.page else None
    return RecognizedPhoto(page=page, role=page_role(page), rec=rec)


def _split_task_columns(page: VisionPage) -> VisionPage:
    tasks = [
        t.model_copy(update={"student_solution_steps": split_columns(t.student_solution_steps)})
        for t in page.tasks
    ]
    return page.model_copy(update={"tasks": tasks})


def split_pages(photos: list[RecognizedPhoto], textbook: list[VisionTask]) -> AlbumPages:
    """Учебник даёт условия, тетрадь — решения; условия копятся к уже известным."""
    known = list(textbook)
    notebook_pages: list[list[VisionTask]] = []
    new_textbook: list[VisionTask] = []
    comment: str | None = None
    for photo in photos:
        page = photo.page
        if page is None:
            continue
        if photo.role == "textbook":
            new_textbook.extend(merge_textbook([], page.tasks))
            known = merge_textbook(known, page.tasks)
        elif photo.role == "notebook":
            notebook_pages.append(page.tasks)
        elif page.page_comment:
            comment = page.page_comment
    return AlbumPages(
        notebook=_attach_continuations(notebook_pages),
        textbook=known,
        new_textbook=new_textbook,
        comment=comment,
    )


def _attach_continuations(pages: list[list[VisionTask]]) -> list[VisionTask]:
    """Задания тетради по порядку фото; страница-продолжение приклеена к своему заданию.

    Живой альбом 14.09: «Ответ: было — 35 луковиц» к №57 — на отдельной странице, и это фото
    пришло раньше страницы с №57, а структуризатор назвал его «№1». Страница из одного задания
    без номера, но с ответом — продолжение задания, которым кончается другая страница, если оно с
    номером и без ответа. Приклеиваем, только когда и продолжение, и такое задание в альбоме
    одни: чужой ответ в задании хуже отдельного «№1» (ревью). Без ответа не приклеиваем:
    повторный прогон того же фото дал вместо ответа выдуманные дроби, и они сломали бы сверку
    последней строки №57.
    """
    tasks: list[VisionTask] = []
    continuations: list[int] = []
    unfinished: list[int] = []  # последнее задание страницы: с номером, без ответа
    for page in pages:
        if len(page) == 1 and not page[0].number_on_page and _has_answer(page[0]):
            continuations.append(len(tasks))
        elif page and page[-1].number_on_page and not _has_answer(page[-1]):
            unfinished.append(len(tasks) + len(page) - 1)
        tasks.extend(page)
    if len(continuations) != 1 or len(unfinished) != 1:
        return tasks
    [continuation], [target] = continuations, unfinished
    tasks[target] = _merge_continuation(tasks[target], tasks[continuation])
    return [task for index, task in enumerate(tasks) if index != continuation]


def _has_answer(task: VisionTask) -> bool:
    return bool((task.student_answer or "").strip())


def _merge_continuation(previous: VisionTask, continuation: VisionTask) -> VisionTask:
    return previous.model_copy(
        update={
            "student_solution_steps": [
                *previous.student_solution_steps,
                *continuation.student_solution_steps,
            ],
            "student_answer": continuation.student_answer,
            "confidence": min(previous.confidence, continuation.confidence),
        }
    )


async def check_task(
    llm: VisionAndChatClient,
    task: VisionTask,
    models: CheckModels,
    cache: FileCache | None,
) -> TaskCheck:
    ref: RefSolution | None = None
    solved: SolvedTask | None = None
    solver_result: LLMResult | None = None
    ref_status: RefStatus = "no_condition"
    if task.task_text.strip():
        try:
            solved, solver_result = await solve_task(
                llm, task.task_text, model=models.solver, cache=cache
            )
            if solved.ref_ok:
                ref = solved.solution
            ref_status = "ok" if solved.ref_ok else "ref_not_verified"
        except StructuredOutputError:
            logger.warning("solver failed for task %s", task.number)
            ref_status = "solver_failed"
    if ref is not None:
        result = grade(
            task.student_solution_steps, task.student_answer, ref, condition=task.task_text
        )
    else:
        result = validator_only_grade(task.student_solution_steps, condition=task.task_text)
    return TaskCheck(
        task=task,
        ref=ref,
        grade=result,
        ref_status=ref_status,
        solved=solved,
        solver_result=solver_result,
    )


def validator_only_grade(steps: list[str], *, condition: str | None = None) -> GradeResult:
    """Столбик примеров без условия: проверка — только детерминированный пересчёт."""
    condition = condition or None
    return grade_by_lines(check_steps(steps, condition=condition), condition=condition)
