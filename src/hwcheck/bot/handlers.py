"""Сценарий бота: фото → проверка → кнопки «Разобрать» → диалог тьютора.

Логика детерминированная (FSM в fsm.py); LLM-шаги вызываются из pipeline.
Каждый вызов компонента логируется в EventLog (конкурсная метрика + антифрод).
"""

import contextlib
import logging
import time
from pathlib import Path

from hwcheck.bot.check import (
    CheckModels,
    RecognizedPhoto,
    recognize_photo,
    split_pages,
    validator_only_grade,
)
from hwcheck.bot.clarify import (
    MAX_ATTEMPTS,
    apply_sign,
    apply_text,
    apply_word,
    parse_sign_payload,
    plan_clarifications,
    question,
    retry_prompt,
)
from hwcheck.bot.crops import crop_word
from hwcheck.bot.fsm import ChatState, CheckedTask, Clarification, StateStore
from hwcheck.bot.max_api import MaxClient
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding
from hwcheck.bot.pages import (
    MAX_PHOTOS,
    attach_conditions,
    describe_tasks,
    task_label,
    textbook_is_fresh,
)
from hwcheck.bot.summary import clarified_line, review_header, task_line
from hwcheck.bot.summary import lower as _lower
from hwcheck.bot.summary import remaining_buttons as _remaining_buttons
from hwcheck.config import Settings
from hwcheck.db.findings import FindingRecord, FindingsRepository
from hwcheck.events import EventLog, anonymize, current_trace_id, trace
from hwcheck.llm.gigachat_client import GigaChatClient
from hwcheck.photos import PhotoStore
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import FileCache, RefSolution
from hwcheck.pipeline.tutor import TutorSession, tutor_reply
from hwcheck.subjects.base import Finding, Reference, SubjectTask, TaskResult, Trust
from hwcheck.subjects.math.module import _pseudo_ref as _pseudo_ref  # ре-экспорт для тестов
from hwcheck.subjects.math.module import findings_from_grade, to_subject_task
from hwcheck.subjects.registry import SubjectDeps, module_for

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
REVIEW_HINT = "Выбери задание для разбора 👇 Или пришли фото новой домашки 📸"
REVIEW_DONE = "Эту домашку я уже проверил 👍 Пришли фото следующей — проверю 📸"


class Bot:
    def __init__(
        self,
        max_client: MaxClient,
        llm: GigaChatClient,
        store: StateStore,
        events: EventLog,
        settings: Settings,
        *,
        photos: PhotoStore | None = None,
        onboarding: Onboarding | None = None,
        subjects: SubjectDeps | None = None,
        findings: FindingsRepository | None = None,
    ) -> None:
        self._max = max_client
        self._llm = llm
        self._store = store
        self._events = events
        self._settings = settings
        self._photos = photos
        # None — ONBOARDING_REQUIRED=false: проверка без онбординга, как до этапа 2
        self._onboarding = onboarding
        self._findings = findings
        self._cache = FileCache(Path(".cache/solver"))
        # математика — единственный реализованный предмет; профиль ученика определит код позже
        self._module = module_for("math", subjects or SubjectDeps(llm, self._models, self._cache))

    @property
    def _models(self) -> CheckModels:
        return CheckModels(
            vision=self._settings.vision_model,
            structure=self._settings.tutor_model,
            solver=self._settings.solver_model,
        )

    async def handle_update(self, update: MaxUpdate) -> None:
        # один trace_id на все вызовы компонентов по апдейту (антифрод, Прил. 2 п. 5)
        with trace():
            try:
                await self._dispatch(update)
            except Exception as exc:
                # сбой хранилища/сети посреди диалога: «попробуй ещё раз», а не тишина
                logger.exception("update failed: %s", update.update_type)
                self._events.log(
                    "update_failed", user_id=update.effective_user_id, error=type(exc).__name__
                )
                await self._reply_retry(update)

    async def _reply_retry(self, update: MaxUpdate) -> None:
        try:
            if update.callback is not None and update.callback.callback_id:
                # без ответа на callback кнопка в MAX «крутится» у ребёнка бесконечно
                with contextlib.suppress(Exception):
                    await self._max.answer_callback(update.callback.callback_id)
            if update.effective_chat_id is not None:
                await self._max.send_message(update.effective_chat_id, RETRY)
        except Exception:
            logger.exception("retry reply failed")

    async def _dispatch(self, update: MaxUpdate) -> None:
        chat_id = update.effective_chat_id
        if chat_id is None:
            return
        user_id = update.effective_user_id
        if self._onboarding is not None:
            # онбординг первым: до согласия фото не скачивается (спецификация онбординга §10.2)
            route = await self._onboarding.route(update)
            if isinstance(route, CheckPhotos):
                await self._on_photo(chat_id, user_id, route.urls)
                return
            if route == "handled":
                return
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
        except Exception as exc:
            # ребёнок не должен остаться наедине с «Проверяю...» и тишиной
            logger.exception("photo processing failed")
            self._events.log("check_failed", user_id=user_id, error=type(exc).__name__)
            await self._max.send_message(chat_id, RETRY)

    def _save_photo(self, user_id: int | None, image: bytes) -> str | None:
        """Сбой диска не должен ломать проверку: без фото разбор хуже, но ребёнок получит ответ."""
        if self._photos is None:
            return None
        try:
            return self._photos.save(anonymize(user_id), image)
        except OSError:
            logger.exception("photo save failed")
            return None

    async def _recognize(
        self, user_id: int | None, image: bytes, photo: str | None
    ) -> RecognizedPhoto:
        recognized = await recognize_photo(self._llm, image, self._models)
        page, role, rec = recognized.page, recognized.role, recognized.rec
        # структура страницы без содержимого — чтобы разбирать спорные роли по логу;
        # сама транскрипция (текст ребёнка) — только в dev
        summary = [
            (
                t.number,
                t.number_on_page,
                bool(t.task_text.strip()),
                len(t.student_solution_steps),
                t.student_answer is not None,
            )
            for t in (page.tasks if page else [])
        ]
        logger.info(
            "page role=%s tasks(number, on_page, has_text, n_steps, has_answer)=%s", role, summary
        )
        if self._settings.environment == "dev":
            logger.info("transcript: %s", rec.raw)
        self._events.log(
            "vision_recognized",
            user_id=user_id,
            component="vision_two_stage",
            calls=rec.attempts + 1,
            tokens=rec.tokens_in + rec.tokens_out,
            n_tasks=len(page.tasks) if page else 0,
            role=role,
            photo=photo,
        )
        return recognized

    async def _recognize_all(
        self, user_id: int | None, urls: list[str]
    ) -> tuple[list[RecognizedPhoto], list[str]]:
        """Сбой одного фото (сеть, vision) не теряет остальные; упали все — наверх.

        Пути идут в одном порядке с результатами (album order): это и есть индекс,
        на который ссылается `Word.photo_index` для кропа в уточняющем вопросе.
        """
        results: list[RecognizedPhoto] = []
        paths: list[str] = []
        failed = 0
        for url in urls:
            photo: str | None = None
            try:
                image = await self._max.download(url)
                # до vision: фото, на которых распознавание упало, — самые ценные для разбора
                photo = self._save_photo(user_id, image)
                results.append(await self._recognize(user_id, image, photo))
                paths.append(photo or "")
            except Exception as exc:
                failed += 1
                logger.exception("photo failed: %s", url.split("?")[0])
                self._events.log(
                    "photo_failed", user_id=user_id, error=type(exc).__name__, photo=photo
                )
        if urls and failed == len(urls):
            raise RuntimeError("all photos failed")
        return results, paths

    async def _process_photos(self, chat_id: int, user_id: int | None, urls: list[str]) -> None:
        """Все фото сообщения: учебник даёт условия, тетрадь — решения.

        Проверяются только задания тетради; условия учебника запоминаются в
        состоянии чата (TTL), так что тетрадь может прийти и следующим сообщением.
        """
        state = await self._store.get(chat_id)
        known = list(state.textbook_tasks) if textbook_is_fresh(state.textbook_saved_at) else []
        recognized, photo_paths = await self._recognize_all(user_id, urls)
        album = split_pages(recognized, known)
        notebook, textbook = album.notebook, album.textbook
        new_textbook, comment = album.new_textbook, album.comment
        if not notebook:
            if new_textbook:
                remembered = state.model_copy(
                    update={"textbook_tasks": textbook, "textbook_saved_at": time.time()}
                )
                await self._store.set(chat_id, remembered)
                numbers = describe_tasks(new_textbook)
                await self._max.send_message(chat_id, TEXTBOOK_ONLY.format(numbers=numbers))
            else:
                await self._max.send_message(
                    chat_id, UNREADABLE + (f"\n({comment})" if comment else "")
                )
            return
        checked = [
            await self._check_task(user_id, task) for task in attach_conditions(notebook, textbook)
        ]
        plan = plan_clarifications(checked)
        new_state = ChatState(
            phase="clarifying" if plan else "review",
            tasks=checked,
            textbook_tasks=textbook,
            textbook_saved_at=time.time() if textbook else None,
            clarifications=plan,
            photo_paths=photo_paths,
        )
        await self._store.set(chat_id, new_state)
        await self._send_review(chat_id, new_state)
        if plan:
            await self._ask_clarification(chat_id, user_id, new_state)

    async def _check_task(self, user_id: int | None, task: VisionTask) -> CheckedTask:
        subject_task = to_subject_task(task)
        [result] = await self._module.check([subject_task], [])
        payload = result.payload
        if payload.get("solver_from_cache") is not None:
            self._events.log(
                "solver_call",
                user_id=user_id,
                component="solver",
                from_cache=payload["solver_from_cache"],
                tokens=payload["solver_tokens"],
            )
        grade = GradeResult.model_validate(payload["grade"])
        ref = (
            RefSolution.model_validate(result.reference.payload["ref"])
            if result.reference is not None and "ref" in result.reference.payload
            else None
        )
        if result.reference is not None:
            self._events.log(
                "reference_resolved",
                user_id=user_id,
                subject=self._module.code,
                origin=result.reference.origin,
                trust=result.reference.trust,
            )
        # что стало с эталоном — вторая половина ответа на вопрос «почему не уверен»
        self._events.log(
            "task_checked",
            user_id=user_id,
            component="validator",
            verdict=grade.verdict,
            reason=grade.uncertain_reason,
            ref_status=payload["ref_status"],
            n_steps=len(task.student_solution_steps),
            n_parsed=sum(1 for c in grade.line_checks if c.status in ("ok", "mismatch")),
            has_answer=bool((task.student_answer or "").strip()),
        )
        await self._record_findings(user_id, subject_task, result.findings)
        return CheckedTask(
            task=task,
            ref=ref,
            grade=grade,
            findings=result.findings,
            ref_status=payload["ref_status"],
        )

    async def _record_findings(
        self, user_id: int | None, task: SubjectTask, findings: list[Finding]
    ) -> None:
        for finding in findings:
            self._events.log(
                "finding_created",
                user_id=user_id,
                subject=self._module.code,
                kind=finding.kind,
                strength=finding.strength,
                rule_code=finding.rule_code,
            )
        user_hash = anonymize(user_id)
        if self._findings is None or user_hash is None or not findings:
            return
        records = [
            FindingRecord(
                user_hash=user_hash,
                subject=self._module.code,
                trace_id=current_trace_id(),
                task_number=task.number,
                kind=f.kind,
                strength=f.strength,
                rule_code=f.rule_code,
                confirmed=f.confirmed,
            )
            for f in findings
        ]
        try:
            await self._findings.save(records)
        except Exception:
            # аналитика не должна ломать проверку: ребёнок ждёт сводку
            logger.exception("findings save failed")

    async def _send_review(self, chat_id: int, state: ChatState) -> None:
        asked = {c.task_index for c in state.clarifications}
        lines = []
        buttons = []
        for i, item in enumerate(state.tasks):
            if i in asked:
                lines.append(f"{task_label(item.task)} — уточню у тебя одну деталь ✍️")
                continue
            line, button = task_line(i, item)
            lines.append(line)
            if button:
                buttons.append(button)
        header = review_header(state)
        await self._max.send_message(chat_id, header + "\n".join(lines), buttons=buttons or None)

    # --- уточняющие вопросы (bot/clarify.py): код ведёт очередь, ответ пересчитывается ---

    async def _ask_clarification(self, chat_id: int, user_id: int | None, state: ChatState) -> None:
        clarification = state.clarifications[0]
        item = state.tasks[clarification.task_index]
        text, buttons = question(item, clarification)
        if clarification.kind == "word":
            image_token = await self._word_image_token(state, item, clarification)
            self._events.log(
                "clarification_asked",
                user_id=user_id,
                kind=clarification.kind,
                with_image=image_token is not None,
            )
            await self._max.send_message(chat_id, text, buttons=buttons, image_token=image_token)
            return
        self._events.log(
            "clarification_asked",
            user_id=user_id,
            kind=clarification.kind,
            reason=item.grade.uncertain_reason,
        )
        await self._max.send_message(chat_id, text, buttons=buttons)

    async def _word_image_token(
        self, state: ChatState, item: CheckedTask, clarification: Clarification
    ) -> str | None:
        """Кроп слова для вопроса «здесь написано …?»: любой сбой — вопрос уходит текстом."""
        if clarification.finding_index is None:
            return None
        finding = item.findings[clarification.finding_index]
        word = finding.word
        if word is None or word.box is None:
            return None
        try:
            path = state.photo_paths[word.photo_index]
            image = self._photos.load(path) if self._photos is not None else None
            if image is None:
                return None
            crop = crop_word(image, word.box)
            return await self._max.upload_image(crop)
        except Exception:
            logger.warning("word crop/upload failed", exc_info=True)
            return None

    async def _answer_clarification(
        self, chat_id: int, user_id: int | None, state: ChatState, updated: CheckedTask | None
    ) -> None:
        clarification = state.clarifications[0]
        before = state.tasks[clarification.task_index]
        rest = state.clarifications[1:]
        tasks = list(state.tasks)
        if updated is None and clarification.attempts + 1 < MAX_ATTEMPTS:
            retried = clarification.model_copy(update={"attempts": clarification.attempts + 1})
            await self._store.set(
                chat_id, state.model_copy(update={"clarifications": [retried, *rest]})
            )
            text, buttons = retry_prompt(clarification)
            await self._max.send_message(chat_id, text, buttons=buttons)
            return
        self._events.log(
            "clarification_answered",
            user_id=user_id,
            user_initiated=True,
            kind=clarification.kind,
            understood=updated is not None,
            verdict_before=before.grade.verdict,
            verdict_after=(updated or before).grade.verdict,
        )
        if updated is None:
            message = f"Хорошо, оставлю {_lower(task_label(before.task))} как есть 🤔"
            buttons = None
        else:
            tasks[clarification.task_index] = updated
            if clarification.kind == "word" and clarification.finding_index is not None:
                # спецификация каркаса §8: доля «нет» — мера ложных срабатываний OCR по предмету
                confirmed = updated.findings[clarification.finding_index].confirmed
                self._events.log(
                    "finding_confirmed",
                    user_id=user_id,
                    user_initiated=True,
                    answer="yes" if confirmed else "no",
                )
            # отдельное событие: задание уже учтено в task_checked, в отчёте не дублируем
            self._events.log(
                "task_clarified",
                user_id=user_id,
                component="validator",
                kind=clarification.kind,
                verdict=updated.grade.verdict,
                reason=updated.grade.uncertain_reason,
            )
            message, button = clarified_line(clarification.task_index, updated)
            buttons = [button] if button else None
        state = state.model_copy(
            update={
                "tasks": tasks,
                "clarifications": rest,
                "phase": "clarifying" if rest else "review",
            }
        )
        await self._store.set(chat_id, state)
        await self._max.send_message(chat_id, message, buttons=buttons)
        if rest:
            await self._ask_clarification(chat_id, user_id, state)

    async def _on_callback(
        self, chat_id: int, user_id: int | None, payload: str, callback_id: str
    ) -> None:
        self._events.log("button_pressed", user_id=user_id, user_initiated=True, payload=payload)
        state = await self._store.get(chat_id)
        if payload.startswith("clarify:"):
            await self._max.answer_callback(callback_id)
            parsed = parse_sign_payload(payload)
            if parsed is None or state.phase != "clarifying" or not state.clarifications:
                return
            token, key = parsed
            current = state.clarifications[0]
            if token != current.token:
                return  # кнопка прошлого вопроса: следующий вопрос она не отвечает
            item = state.tasks[current.task_index]
            updated = (
                apply_word(item, current, key)
                if current.kind == "word"
                else apply_sign(item, current, key)
            )
            await self._answer_clarification(chat_id, user_id, state, updated)
            return
        index = (
            _parse_tutor_index(payload, len(state.tasks)) if payload.startswith("tutor:") else None
        )
        if index is None:
            await self._max.answer_callback(callback_id)
            return
        item = state.tasks[index]
        await self._max.answer_callback(
            callback_id, notification=f"Разбираем {_lower(task_label(item.task))}"
        )
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
        # разбор другого задания снимает оставшиеся вопросы (задания остаются «не уверен»)
        state = state.model_copy(
            update={
                "phase": "tutoring",
                "tutor": session,
                "tutoring_index": index,
                "clarifications": [],
            }
        )
        await self._store.set(chat_id, state)
        await self._max.send_message(chat_id, reply)

    async def _start_tutoring(self, user_id: int | None, item: CheckedTask) -> TutorSession:
        result = task_result_of(0, item)
        session = await self._module.start_tutoring(result, to_subject_task(item.task), kb=None)
        if session.error is not None:
            self._events.log(
                "error_classified",
                user_id=user_id,
                component="classifier",
                error_type=session.error.error_type,
            )
        return session

    async def _on_text(self, chat_id: int, user_id: int | None, text: str) -> None:
        self._events.log("message_received", user_id=user_id, user_initiated=True)
        state = await self._store.get(chat_id)
        if state.phase == "clarifying" and state.clarifications:
            current = state.clarifications[0]
            updated = apply_text(state.tasks[current.task_index], current, text)
            await self._answer_clarification(chat_id, user_id, state, updated)
            return
        if state.phase == "review" and state.tasks:
            # после сводки приветствие выглядит так, будто бот всё забыл (живой альбом 14.09)
            remaining = _remaining_buttons(state)
            text = REVIEW_HINT if remaining else REVIEW_DONE
            await self._max.send_message(chat_id, text, buttons=remaining or None)
            return
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


def task_result_of(index: int, item: CheckedTask) -> TaskResult:
    """`TaskResult` для тьютора из уже посчитанного `CheckedTask`.

    Доверие эталону — по `item.ref_status`, а не по одному факту «эталон есть»:
    `checked.ref` из `bot/check.py` бывает не пуст только когда солвер сам себя проверил
    (`ref_status == "ok"`), но это поле не должно тихо подменяться в других сценариях.
    """
    subject_task = to_subject_task(item.task)
    trust: Trust = "verified" if item.ref_status == "ok" else "unverified"
    reference = (
        Reference(
            task_number=subject_task.number,
            origin="derived",
            trust=trust,
            payload={"ref": item.ref.model_dump()},
        )
        if item.ref is not None
        else None
    )
    return TaskResult(
        task_index=index,
        findings=item.findings or findings_from_grade(index, item.grade),
        reference=reference,
        payload={"grade": item.grade.model_dump(), "ref_status": item.ref_status},
    )


def _parse_tutor_index(payload: str, n_tasks: int) -> int | None:
    """Payload недоверенный: только 'tutor:<цифры>' в границах списка."""
    raw = payload.split(":", 1)[1] if ":" in payload else ""
    if not raw.isdigit():
        return None
    index = int(raw)
    return index if index < n_tasks else None


def _validator_only_grade(steps: list[str], *, condition: str | None = None) -> GradeResult:
    """Столбик примеров без условия: проверка — только детерминированный пересчёт."""
    return validator_only_grade(steps, condition=condition)
