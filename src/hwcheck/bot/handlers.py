"""Сценарий бота: фото → проверка → кнопки «Разобрать» → диалог тьютора.

Логика детерминированная (FSM в fsm.py); LLM-шаги вызываются из pipeline.
Каждый вызов компонента логируется в EventLog (конкурсная метрика + антифрод).
"""

import logging
import time
from pathlib import Path

from hwcheck.bot.fsm import ChatState, CheckedTask, StateStore
from hwcheck.bot.max_api import MaxClient, callback_button
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.pages import (
    MAX_PHOTOS,
    PageRole,
    attach_conditions,
    format_numbers,
    merge_textbook,
    page_role,
    textbook_is_fresh,
)
from hwcheck.config import Settings
from hwcheck.events import EventLog
from hwcheck.llm.gigachat_client import GigaChatClient
from hwcheck.pipeline.classifier import classify_error
from hwcheck.pipeline.grade import GradeResult, grade
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import FileCache, RefSolution, StructuredOutputError, solve_task
from hwcheck.pipeline.tutor import TutorSession, tutor_reply
from hwcheck.pipeline.validator import check_steps
from hwcheck.pipeline.vision import recognize_page_two_stage

logger = logging.getLogger(__name__)

WELCOME = (
    "Привет! Я проверяю домашку по математике. 📚\n"
    "Пришли фото страницы тетради с решением — я проверю и помогу разобрать ошибки."
)
CHECKING = "Проверяю... 🔍 Обычно это занимает меньше минуты."
UNREADABLE = (
    "Не смог разобрать фото 😕 Попробуй переснять: страница целиком, "
    "вертикально, при хорошем свете."
)
RETRY = "Что-то пошло не так с моей стороны 😔 Попробуй ещё раз через минуту."
TEXTBOOK_ONLY = (
    "Вижу страницу учебника ({numbers}) 📖 Пришли фото тетради с решением — "
    "проверю по этим условиям."
)


class Bot:
    def __init__(
        self,
        max_client: MaxClient,
        llm: GigaChatClient,
        store: StateStore,
        events: EventLog,
        settings: Settings,
    ) -> None:
        self._max = max_client
        self._llm = llm
        self._store = store
        self._events = events
        self._settings = settings
        self._cache = FileCache(Path(".cache/solver"))

    async def handle_update(self, update: MaxUpdate) -> None:
        chat_id = update.effective_chat_id
        if chat_id is None:
            return
        user_id = update.effective_user_id
        if update.update_type == "bot_started":
            self._events.log("bot_started", user_id=user_id, user_initiated=True)
            await self._max.send_message(chat_id, WELCOME)
        elif update.update_type == "message_created" and update.message is not None:
            if update.message.image_urls:
                await self._on_photo(chat_id, user_id, update.message.image_urls)
            elif update.message.body and update.message.body.text:
                await self._on_text(chat_id, user_id, update.message.body.text)
        elif update.update_type == "message_callback" and update.callback is not None:
            await self._on_callback(
                chat_id, user_id, update.callback.payload or "", update.callback.callback_id or ""
            )

    async def _on_photo(self, chat_id: int, user_id: int | None, urls: list[str]) -> None:
        dropped = max(0, len(urls) - MAX_PHOTOS)
        self._events.log(
            "homework_uploaded",
            user_id=user_id,
            user_initiated=True,
            n_photos=len(urls),
            n_dropped=dropped,
        )
        hint = f" Фото больше {MAX_PHOTOS} — возьму первые {MAX_PHOTOS}." if dropped else ""
        await self._max.send_message(chat_id, CHECKING + hint)
        try:
            await self._process_photos(chat_id, user_id, urls[:MAX_PHOTOS])
        except Exception:
            # ребёнок не должен остаться наедине с «Проверяю...» и тишиной
            logger.exception("photo processing failed")
            self._events.log("check_failed", user_id=user_id)
            await self._max.send_message(chat_id, RETRY)

    async def _recognize(self, user_id: int | None, url: str) -> tuple[VisionPage | None, PageRole]:
        image = await self._max.download(url)
        rec = await recognize_page_two_stage(
            self._llm,
            image,
            vision_model=self._settings.vision_model,
            structure_model=self._settings.tutor_model,
        )
        role = page_role(rec.page)
        # структура страницы без содержимого — чтобы разбирать спорные роли по логу;
        # сама транскрипция (текст ребёнка) — только в dev
        summary = [
            (
                t.number,
                bool(t.task_text.strip()),
                len(t.student_solution_steps),
                t.student_answer is not None,
            )
            for t in (rec.page.tasks if rec.page else [])
        ]
        logger.info("page role=%s tasks(number, has_text, n_steps, has_answer)=%s", role, summary)
        if self._settings.environment == "dev":
            logger.info("transcript: %s", rec.raw)
        self._events.log(
            "vision_recognized",
            user_id=user_id,
            component="vision_two_stage",
            calls=rec.attempts + 1,
            tokens=rec.tokens_in + rec.tokens_out,
            n_tasks=len(rec.page.tasks) if rec.page else 0,
            role=role,
        )
        return rec.page, role

    async def _recognize_all(
        self, user_id: int | None, urls: list[str]
    ) -> list[tuple[VisionPage | None, PageRole]]:
        """Сбой одного фото (сеть, vision) не теряет остальные; упали все — наверх."""
        results: list[tuple[VisionPage | None, PageRole]] = []
        failed = 0
        for url in urls:
            try:
                results.append(await self._recognize(user_id, url))
            except Exception:
                failed += 1
                logger.exception("photo failed: %s", url.split("?")[0])
        if urls and failed == len(urls):
            raise RuntimeError("all photos failed")
        return results

    async def _process_photos(self, chat_id: int, user_id: int | None, urls: list[str]) -> None:
        """Все фото сообщения: учебник даёт условия, тетрадь — решения.

        Проверяются только задания тетради; условия учебника запоминаются в
        состоянии чата (TTL), так что тетрадь может прийти и следующим сообщением.
        """
        state = await self._store.get(chat_id)
        textbook = list(state.textbook_tasks) if textbook_is_fresh(state.textbook_saved_at) else []
        notebook: list[VisionTask] = []
        new_numbers: list[int] = []
        comment: str | None = None
        for page, role in await self._recognize_all(user_id, urls):
            if page is None:
                continue
            if role == "textbook":
                new_numbers.extend(t.number for t in merge_textbook([], page.tasks))
                textbook = merge_textbook(textbook, page.tasks)
            elif role == "notebook":
                notebook.extend(page.tasks)
            elif page.page_comment:
                comment = page.page_comment
        if not notebook:
            if new_numbers:
                remembered = state.model_copy(
                    update={"textbook_tasks": textbook, "textbook_saved_at": time.time()}
                )
                await self._store.set(chat_id, remembered)
                numbers = format_numbers(new_numbers)
                await self._max.send_message(chat_id, TEXTBOOK_ONLY.format(numbers=numbers))
            else:
                await self._max.send_message(
                    chat_id, UNREADABLE + (f"\n({comment})" if comment else "")
                )
            return
        checked = [
            await self._check_task(user_id, task) for task in attach_conditions(notebook, textbook)
        ]
        new_state = ChatState(
            phase="review",
            tasks=checked,
            textbook_tasks=textbook,
            textbook_saved_at=time.time() if textbook else None,
        )
        await self._store.set(chat_id, new_state)
        await self._send_review(chat_id, new_state)

    async def _check_task(self, user_id: int | None, task: VisionTask) -> CheckedTask:
        ref: RefSolution | None = None
        if task.task_text.strip():
            try:
                solved, llm_result = await solve_task(
                    self._llm,
                    task.task_text,
                    model=self._settings.solver_model,
                    cache=self._cache,
                )
                self._events.log(
                    "solver_call",
                    user_id=user_id,
                    component="solver",
                    from_cache=solved.from_cache,
                    tokens=(llm_result.tokens_in + llm_result.tokens_out) if llm_result else 0,
                )
                if solved.ref_ok:
                    ref = solved.solution
            except StructuredOutputError:
                logger.warning("solver failed for task %s", task.number)
        if ref is not None:
            result = grade(task.student_solution_steps, task.student_answer, ref)
        else:
            result = _validator_only_grade(task.student_solution_steps)
        self._events.log(
            "task_checked",
            user_id=user_id,
            component="validator",
            verdict=result.verdict,
        )
        return CheckedTask(task=task, ref=ref, grade=result)

    async def _send_review(self, chat_id: int, state: ChatState) -> None:
        lines = []
        buttons = []
        for i, item in enumerate(state.tasks):
            number = item.task.number
            if item.grade.verdict == "correct":
                lines.append(f"№{number} — верно ✅")
            elif item.grade.verdict == "wrong":
                where = (
                    f" (строка {item.grade.first_error_line})"
                    if item.grade.first_error_line
                    else ""
                )
                lines.append(f"№{number} — есть ошибка{where} ❌")
                buttons.append([callback_button(f"Разобрать №{number}", f"tutor:{i}")])
            else:
                lines.append(f"№{number} — не уверен, лучше показать взрослому 🤔")
        correct = sum(1 for t in state.tasks if t.grade.verdict == "correct")
        header = f"Проверил! {correct} из {len(state.tasks)} верно.\n"
        await self._max.send_message(chat_id, header + "\n".join(lines), buttons=buttons or None)

    async def _on_callback(
        self, chat_id: int, user_id: int | None, payload: str, callback_id: str
    ) -> None:
        self._events.log("button_pressed", user_id=user_id, user_initiated=True, payload=payload)
        state = await self._store.get(chat_id)
        index = (
            _parse_tutor_index(payload, len(state.tasks)) if payload.startswith("tutor:") else None
        )
        if index is None:
            await self._max.answer_callback(callback_id)
            return
        item = state.tasks[index]
        await self._max.answer_callback(callback_id, notification=f"Разбираем №{item.task.number}")
        try:
            session = await self._start_tutoring(user_id, item)
            reply, session = await tutor_reply(
                self._llm, session, "Помоги найти ошибку", model=self._settings.tutor_model
            )
        except Exception:
            logger.exception("tutoring start failed")
            await self._max.send_message(chat_id, RETRY)
            return
        self._events.log(
            "tutor_reply", user_id=user_id, component="tutor", hint_level=session.hint_level
        )
        state = state.model_copy(
            update={"phase": "tutoring", "tutor": session, "tutoring_index": index}
        )
        await self._store.set(chat_id, state)
        await self._max.send_message(chat_id, reply)

    async def _start_tutoring(self, user_id: int | None, item: CheckedTask) -> TutorSession:
        ref = item.ref or _pseudo_ref(item.grade)
        error = None
        if item.ref is not None:
            try:
                error = await classify_error(
                    self._llm,
                    item.task.task_text,
                    item.task.student_solution_steps,
                    item.task.student_answer,
                    item.ref,
                    item.grade,
                    model=self._settings.tutor_model,
                )
                self._events.log(
                    "error_classified",
                    user_id=user_id,
                    component="classifier",
                    error_type=error.error_type,
                )
            except StructuredOutputError:
                logger.warning("classifier failed")
        return TutorSession(
            task_text=item.task.task_text or "\n".join(item.task.student_solution_steps),
            student_steps=item.task.student_solution_steps,
            student_answer=item.task.student_answer,
            ref=ref,
            error=error,
            first_error_line=item.grade.first_error_line,
        )

    async def _on_text(self, chat_id: int, user_id: int | None, text: str) -> None:
        self._events.log("message_received", user_id=user_id, user_initiated=True)
        state = await self._store.get(chat_id)
        if state.phase != "tutoring" or state.tutor is None:
            await self._max.send_message(chat_id, WELCOME)
            return
        try:
            reply, session = await tutor_reply(
                self._llm, state.tutor, text, model=self._settings.tutor_model
            )
        except Exception:
            logger.exception("tutor reply failed")
            await self._max.send_message(chat_id, RETRY)
            return
        self._events.log(
            "tutor_reply",
            user_id=user_id,
            component="tutor",
            hint_level=session.hint_level,
            resolved=session.resolved,
        )
        if session.resolved:
            self._events.log("error_fixed", user_id=user_id, user_initiated=True)
            resolved = (
                [*state.resolved_indices, state.tutoring_index]
                if state.tutoring_index is not None
                else state.resolved_indices
            )
            state = state.model_copy(
                update={
                    "phase": "review",
                    "tutor": None,
                    "tutoring_index": None,
                    "resolved_indices": resolved,
                }
            )
            await self._store.set(chat_id, state)
            await self._max.send_message(chat_id, reply)
            remaining = _remaining_buttons(state)
            if remaining:
                await self._max.send_message(
                    chat_id, "Разберём ещё одну ошибку?", buttons=remaining
                )
        else:
            state = state.model_copy(update={"tutor": session})
            await self._store.set(chat_id, state)
            await self._max.send_message(chat_id, reply)


def _remaining_buttons(state: ChatState) -> list[list[dict[str, str]]]:
    """Кнопки для ещё не разобранных ошибок."""
    return [
        [callback_button(f"Разобрать №{t.task.number}", f"tutor:{i}")]
        for i, t in enumerate(state.tasks)
        if t.grade.verdict == "wrong" and i not in state.resolved_indices
    ]


def _parse_tutor_index(payload: str, n_tasks: int) -> int | None:
    """Payload недоверенный: только 'tutor:<цифры>' в границах списка."""
    raw = payload.split(":", 1)[1] if ":" in payload else ""
    if not raw.isdigit():
        return None
    index = int(raw)
    return index if index < n_tasks else None


def _validator_only_grade(steps: list[str]) -> GradeResult:
    """Столбик примеров без условия: проверка — только детерминированный пересчёт."""
    checks = check_steps(steps)
    mismatches = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    parseable = any(c.status == "ok" for c in checks) or bool(mismatches)
    if not parseable:
        verdict = "uncertain"
    elif mismatches:
        verdict = "wrong"
    else:
        verdict = "correct"
    return GradeResult(
        verdict=verdict,  # type: ignore[arg-type]
        answers_match=None,
        first_error_line=mismatches[0] if mismatches else None,
        slip_lines=[],
        line_checks=checks,
    )


def _pseudo_ref(result: GradeResult) -> RefSolution:
    """Для задания без условия: «эталон» — верное значение первой ошибочной строки."""
    for check in result.line_checks:
        if check.status == "mismatch" and check.values:
            return RefSolution(steps=[], answer=check.values[0], units=None)
    return RefSolution(steps=[], answer="", units=None)
