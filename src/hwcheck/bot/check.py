"""Проверка альбома без мессенджера: распознавание страниц, разбор на учебник и тетрадь,
проверка заданий.

Один код для бота (`handlers.py` добавляет MAX, состояние и журнал событий) и для стенда
сравнения моделей (`hwcheck.bench`): стенд меряет ровно то, что работает в проде.
"""

import contextlib
import logging
import re
from collections.abc import Iterator
from contextvars import ContextVar
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
from hwcheck.pipeline.grade import GradeResult, check_student_steps, grade, grade_by_lines
from hwcheck.pipeline.reading import line_label
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import (
    FileCache,
    RefSolution,
    SolvedTask,
    StructuredOutputError,
    solve_task,
)
from hwcheck.pipeline.vision import RecognizedPage, VisionAndChatClient, recognize_page_two_stage

logger = logging.getLogger(__name__)

RefStatus = Literal["no_condition", "solver_failed", "ref_not_verified", "ok"]


DEFAULT_VISION_PROMPT = "v3"  # писался под GigaChat; сторонним моделям — свой `v4-<модель>`


@dataclass(frozen=True)
class CheckModels:
    # транскрипция фото, когда проверке не назначена другая (`vision_override`). В боте это
    # отечественная модель: модели собираются один раз при старте и о согласии семьи не знают
    vision: str
    structure: str  # разбор транскрипции на задания
    solver: str  # эталонное решение
    # версия промпта транскрипции (`prompts/vision/<версия>.md`) для модели `vision`: промпт
    # следует за моделью, а не за проверкой (стенд 28.09: v3 недобирает на сторонних моделях)
    vision_prompt: str = DEFAULT_VISION_PROMPT


# модель чтения фото и её промпт, назначенные текущей проверке (в боте — согласием родителя).
# Контекст, а не поле: проверки разных чатов идут параллельно, у каждой своя
_vision_override: ContextVar[tuple[str, str] | None] = ContextVar("vision_override", default=None)


@contextlib.contextmanager
def vision_override(model: str | None, prompt: str = DEFAULT_VISION_PROMPT) -> Iterator[None]:
    """Фото внутри блока читает `model` промптом `prompt`; None — модель и промпт из
    `CheckModels`."""
    token = _vision_override.set((model, prompt) if model else None)
    try:
        yield
    finally:
        _vision_override.reset(token)


def vision_of(models: CheckModels) -> str:
    """Модель чтения фото этой проверки. Каждый, кто отправляет фото модели, берёт её отсюда:
    чтение `models.vision` напрямую обходит согласие родителя (ревью 29.09)."""
    override = _vision_override.get()
    return override[0] if override else models.vision


def vision_prompt_of(models: CheckModels) -> str:
    """Версия промпта транскрипции для модели из `vision_of`: та же, что назначена проверке."""
    override = _vision_override.get()
    return override[1] if override else models.vision_prompt


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
        llm,
        image,
        vision_model=vision_of(models),
        structure_model=models.structure,
        transcribe_version=vision_prompt_of(models),
    )
    page = mark_written_numbers(_split_task_columns(rec.page), rec.raw) if rec.page else None
    return RecognizedPhoto(page=page, role=page_role(page), rec=rec)


def _split_task_columns(page: VisionPage) -> VisionPage:
    """Строки решения — по одной строке тетради: модель кладёт две строки в один шаг
    («c = 720, d = 382\\n(720 + 382) − 763 = 339» — живая проверка 30.09), и такой шаг не
    разбирается целиком; колонки делятся после этого."""
    tasks = [
        t.model_copy(
            update={
                "student_solution_steps": split_columns(_one_per_line(t.student_solution_steps))
            }
        )
        for t in page.tasks
    ]
    return page.model_copy(update={"tasks": tasks})


def _one_per_line(steps: list[str]) -> list[str]:
    return [line for step in steps for line in step.splitlines() if line.strip()]


def split_pages(photos: list[RecognizedPhoto], textbook: list[VisionTask]) -> AlbumPages:
    """Учебник даёт условия, тетрадь — решения; условия копятся к уже известным."""
    known = list(textbook)
    notebook_pages: list[list[VisionTask]] = []
    notebook_transcripts: list[str] = []
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
            notebook_transcripts.append(photo.rec.raw)
        elif page.page_comment:
            comment = page.page_comment
    return AlbumPages(
        notebook=_attach_continuations(_join_parts(notebook_pages, notebook_transcripts)),
        textbook=known,
        new_textbook=new_textbook,
        comment=comment,
    )


# «(продолжение)» у номера на странице тетради: ребёнок сам пишет, что это то же задание
_CONTINUED = re.compile(r"продолж", re.IGNORECASE)
_FIRST_ITEMS = {"а", "1"}


def _join_parts(pages: list[list[VisionTask]], transcripts: list[str]) -> list[list[VisionTask]]:
    """Части одного задания — одно задание (живая проверка 30.09: №2.189 стал «Заданием 1» и
    «Заданием 2», №2.191 — двумя «№1791» и «Заданием 119»).

    Часть приклеивается к предыдущему заданию альбома, если она:
    - продолжает пункты: без номера на странице и начинается с пункта «б)», «в)», «2)»…;
    - с тем же номером со страницы, что и предыдущее задание;
    - первая на странице с пометкой «продолжение» и без своего номера.
    Метки пунктов из условия частей переносятся в их первые строки: по ним задание проверяется по
    пунктам и сводка называет пункт.
    """
    flat: list[tuple[int, VisionTask]] = []
    for page_no, (page, transcript) in enumerate(zip(pages, transcripts, strict=True)):
        continued_page = _CONTINUED.search(transcript) is not None
        for position, task in enumerate(page):
            if flat and _continues(flat[-1][1], task, continued_page and position == 0):
                flat[-1] = (flat[-1][0], _joined(flat[-1][1], task))
            else:
                flat.append((page_no, task))
    return [[task for page_no, task in flat if page_no == n] for n in range(len(pages))]


def _continues(previous: VisionTask, task: VisionTask, continued_page: bool) -> bool:
    if task.number_on_page:
        return previous.number_on_page and task.number == previous.number
    if continued_page:
        return True
    item = _leading_item(task)
    return item is not None and item not in _FIRST_ITEMS and item not in _items_of(previous)


def _joined(previous: VisionTask, part: VisionTask) -> VisionTask:
    item = _leading_item(part)
    previous_steps = _labelled(previous.student_solution_steps, _leading_item(previous))
    if item == "б" and previous_steps and line_label(previous_steps[0]) is None:
        previous_steps = _labelled(previous_steps, "а")  # «б)» продолжает пункт «а)» без метки
    condition = "\n".join(t for t in (previous.task_text.strip(), part.task_text.strip()) if t)
    return previous.model_copy(
        update={
            "task_text": condition,
            "student_solution_steps": [
                *previous_steps,
                *_labelled(part.student_solution_steps, item),
            ],
            "student_answer": part.student_answer or previous.student_answer,
            "confidence": min(previous.confidence, part.confidence),
        }
    )


def _leading_item(task: VisionTask) -> str | None:
    """Пункт, с которого начинается часть: по условию («б) 41942 − z …») или первой строке."""
    for text in (task.task_text, *task.student_solution_steps[:1]):
        if text.strip():
            return line_label(text)
    return None


def _items_of(task: VisionTask) -> set[str]:
    labels = (line_label(step) for step in task.student_solution_steps)
    return {label for label in labels if label is not None}


def _labelled(steps: list[str], item: str | None) -> list[str]:
    """Метка пункта в первой строке части, если её там ещё нет."""
    if item is None or not steps or line_label(steps[0]) is not None:
        return list(steps)
    return [f"{item}) {steps[0]}", *steps[1:]]


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
    return grade_by_lines(check_student_steps(steps, condition=condition), condition=condition)
