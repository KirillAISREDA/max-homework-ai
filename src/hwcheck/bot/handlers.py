"""Сценарий бота: фото → проверка → кнопки «Разобрать» → диалог тьютора.

Логика детерминированная (FSM в fsm.py); LLM-шаги вызываются из pipeline.
Каждый вызов компонента логируется в EventLog (конкурсная метрика + антифрод).
"""

import asyncio
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
    _finding,
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
from hwcheck.subjects.base import (
    Finding,
    Reference,
    SubjectModule,
    SubjectPage,
    SubjectTask,
    TaskResult,
    Trust,
)
from hwcheck.subjects.math.module import _pseudo_ref as _pseudo_ref  # ре-экспорт для тестов
from hwcheck.subjects.math.module import findings_from_grade, to_subject_task, to_vision_task
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
OCR_FAILED = (
    "Не смог прочитать тетрадь 😕 Попробуй переснять: страница целиком, без наклона, "
    "при хорошем свете."
)
# предмет есть в профиле, но модуль в этом окружении не собран (нет словаря/OCR)
SUBJECT_UNAVAILABLE = "Проверка по этому предмету пока недоступна 🙏"
NOTHING_TO_TUTOR = "Здесь нечего разбирать — ошибка не подтверждена 🙂"
REVIEW_HINT = "Выбери задание для разбора 👇 Или пришли фото новой домашки 📸"
REVIEW_DONE = "Эту домашку я уже проверил 👍 Пришли фото следующей — проверю 📸"
SOLVER_CACHE_DIR = Path(".cache/solver")


def models_for(settings: Settings) -> CheckModels:
    """Роутинг моделей по шагам (арх. §4) — один источник и для бота, и для раннера."""
    return CheckModels(
        vision=settings.vision_model,
        structure=settings.tutor_model,
        solver=settings.solver_model,
    )


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
        kb_photos: PhotoStore | None = None,
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
        # страницы учебников для базы знаний: свой TTL (365 дней) и свой каталог
        self._kb_photos = kb_photos
        # None — ONBOARDING_REQUIRED=false: проверка без онбординга, как до этапа 2
        self._onboarding = onboarding
        self._findings = findings
        self._cache = FileCache(SOLVER_CACHE_DIR)
        self._deps = subjects or SubjectDeps(llm, self._models, self._cache)
        self._kb = self._deps.kb
        # модуль предмета собирается по первому фото этого предмета и живёт до рестарта
        self._modules: dict[str, SubjectModule] = {}

    def _module_for(self, subject: str) -> SubjectModule:
        """KeyError — предмет не реализован или не настроен в этом окружении (нет словаря/OCR)."""
        if subject not in self._modules:
            self._modules[subject] = module_for(subject, self._deps)
        return self._modules[subject]

    @property
    def _module(self) -> SubjectModule:  # математика — как раньше
        return self._module_for("math")

    @property
    def _models(self) -> CheckModels:
        return models_for(self._settings)

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
                # предмет знает только онбординг (профиль ученика) — без него всё идёт в математику
                await self._on_photo(chat_id, user_id, route.urls, route.subject)
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

    async def _on_photo(
        self, chat_id: int, user_id: int | None, urls: list[str], subject: str = "math"
    ) -> None:
        dropped = max(0, len(urls) - MAX_PHOTOS)
        self._events.log(
            "homework_uploaded",
            user_id=user_id,
            user_initiated=True,
            subject=subject,
            n_photos=len(urls),
            n_dropped=dropped,
        )
        hint = f" Фото больше {MAX_PHOTOS} — возьму первые {MAX_PHOTOS}." if dropped else ""
        await self._max.send_message(chat_id, CHECKING + hint)
        try:
            await self._process_photos(chat_id, user_id, urls[:MAX_PHOTOS], subject)
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

        Пути идут в порядке альбома, по одному на каждое фото сообщения: упавшее занимает своё
        место пустой строкой. Это и есть индекс, на который ссылается `Word.photo_index`
        для кропа в уточняющем вопросе.
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
                paths.append("")  # место в альбоме сохраняется: индексы не должны съезжать
                logger.exception("photo failed: %s", url.split("?")[0])
                self._events.log(
                    "photo_failed", user_id=user_id, error=type(exc).__name__, photo=photo
                )
        if urls and failed == len(urls):
            raise RuntimeError("all photos failed")
        return results, paths

    async def _process_photos(
        self, chat_id: int, user_id: int | None, urls: list[str], subject: str = "math"
    ) -> None:
        """Все фото сообщения: учебник даёт условия, тетрадь — решения.

        Проверяются только задания тетради; условия учебника запоминаются в
        состоянии чата (TTL), так что тетрадь может прийти и следующим сообщением.
        """
        if subject != "math":
            try:
                module = self._module_for(subject)
            except KeyError:
                # предмет в профиле есть, а модуля в этом окружении нет: это ошибка настройки,
                # но ребёнок не должен получить «что-то пошло не так» на каждое фото
                logger.warning("предмет %r не настроен: проверка недоступна", subject)
                self._events.log("subject_unavailable", user_id=user_id, subject=subject)
                await self._max.send_message(chat_id, SUBJECT_UNAVAILABLE)
                return
            await self._process_language_photos(chat_id, user_id, urls, module)
            return
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
            await self._check_task(user_id, task, index)
            for index, task in enumerate(attach_conditions(notebook, textbook))
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

    # --- языки (спецификация §6, §8): страницы приходят от модуля через SubjectPage ---

    async def _process_language_photos(
        self, chat_id: int, user_id: int | None, urls: list[str], module: SubjectModule
    ) -> None:
        """Учебник даёт упражнения (запоминаются на TTL), тетрадь — слова «как написано».

        Упражнение и тетрадь сопоставляются по номеру, единственная пара — друг с другом
        (это делает `module.check`); пересчёта здесь нет, поэтому `CheckedTask.grade` пуст.
        """
        subject = module.code
        state = await self._store.get(chat_id)
        # предмет состояния описывает то, что в нём лежит: у родителя двух детей прошлая
        # домашка может быть по другому предмету — её упражнения этому модулю не подходят
        same_subject = state.subject == subject
        known = (
            list(state.conditions)
            if same_subject and textbook_is_fresh(state.textbook_saved_at)
            else []
        )
        pages, photo_paths, kb_paths = await self._recognize_language_album(module, user_id, urls)
        conditions = {t.number: t for t in known}
        notebook: list[SubjectTask] = []
        ocr_failed = False
        saw_textbook = False
        for index, page in enumerate(pages):
            if page is None:
                continue
            ocr_failed = ocr_failed or page.failure == "ocr_failed"
            if page.role == "textbook":
                saw_textbook = True
                kb_photo = kb_paths[index]
                for task in page.tasks:
                    conditions[task.number] = task.model_copy(update={"photo_path": kb_photo})
            elif page.role == "notebook":
                for task in page.tasks:
                    # координаты слов относятся к своему фото альбома: по ним бот кропает слово
                    words = [w.model_copy(update={"photo_index": index}) for w in task.words]
                    notebook.append(task.model_copy(update={"words": words}))
        remembered = list(conditions.values())
        if not notebook:
            await self._answer_without_notebook(chat_id, remembered, ocr_failed, saw_textbook)
            if remembered:
                # сводка другого предмета вместе с её кнопками «Разобрать» снимается: разбор
                # по ней ушёл бы в чужой модуль
                kept = state if same_subject else ChatState()
                await self._store.set(chat_id, kept.model_copy(update={
                    "conditions": remembered, "textbook_saved_at": time.time(), "subject": subject,
                }))  # fmt: skip
            return
        references = await module.resolve_reference(remembered, self._kb)
        for reference in references:
            self._events.log("reference_resolved", user_id=user_id, subject=subject,
                             origin=reference.origin, trust=reference.trust)  # fmt: skip
        results = await module.check(notebook, references)
        checked = []
        for task, result in zip(notebook, results, strict=True):
            numbered = _numbered_by_condition(task, result.reference, conditions)
            findings = [
                f.model_copy(update={"task_index": result.task_index}) for f in result.findings
            ]
            await self._record_findings(user_id, numbered, findings, subject=subject)
            checked.append(CheckedTask(
                task=to_vision_task(numbered), ref=None, subject_task=numbered, findings=findings,
                reference=result.reference, payload=result.payload,
            ))  # fmt: skip
        plan = plan_clarifications(checked)
        new_state = ChatState(
            phase="clarifying" if plan else "review", tasks=checked, subject=subject,
            conditions=remembered, textbook_saved_at=time.time() if remembered else None,
            clarifications=plan, photo_paths=photo_paths,
        )  # fmt: skip
        await self._store.set(chat_id, new_state)
        await self._send_review(chat_id, new_state)
        if plan:
            await self._ask_clarification(chat_id, user_id, new_state)

    async def _answer_without_notebook(
        self, chat_id: int, remembered: list[SubjectTask], ocr_failed: bool, saw_textbook: bool
    ) -> None:
        """Тетради в альбоме нет: либо её не прочитали, либо пришёл только учебник."""
        if ocr_failed:
            await self._max.send_message(chat_id, OCR_FAILED)
        elif saw_textbook and remembered:
            numbers = describe_tasks([to_vision_task(t) for t in remembered])
            await self._max.send_message(chat_id, TEXTBOOK_ONLY.format(numbers=numbers))
        else:
            await self._max.send_message(chat_id, UNREADABLE)

    async def _recognize_language_album(
        self, module: SubjectModule, user_id: int | None, urls: list[str]
    ) -> tuple[list[SubjectPage | None], list[str], list[str | None]]:
        """Как `_recognize_all`: сбой одного фото не теряет остальные, индексы альбома сохраняются.

        Третий список — страницы учебника в хранилище базы знаний (свой TTL): фото уже в руках,
        второй раз его не скачиваем.
        """
        pages: list[SubjectPage | None] = []
        paths: list[str] = []
        kb_paths: list[str | None] = []
        failed = 0
        for url in urls:
            photo: str | None = None
            kb_photo: str | None = None
            try:
                image = await self._max.download(url)
                photo = self._save_photo(user_id, image)
                page = await module.recognize(image)
                if page.role == "textbook":
                    kb_photo = self._save_kb_photo(user_id, image)
                self._events.log("page_recognized", user_id=user_id, subject=module.code,
                                 role=page.role, n_tasks=len(page.tasks), calls=page.usage.calls,
                                 tokens=page.usage.tokens, photo=photo)  # fmt: skip
                if page.failure == "ocr_failed":
                    self._events.log("ocr_failed", user_id=user_id, subject=module.code)
                pages.append(page)
            except Exception as exc:
                failed += 1
                pages.append(None)
                logger.exception("photo failed: %s", url.split("?")[0])
                self._events.log("photo_failed", user_id=user_id, error=type(exc).__name__,
                                 photo=photo)  # fmt: skip
            paths.append(photo or "")  # место в альбоме сохраняется: индексы не должны съезжать
            kb_paths.append(kb_photo)
        if urls and failed == len(urls):
            raise RuntimeError("all photos failed")
        return pages, paths, kb_paths

    def _save_kb_photo(self, user_id: int | None, image: bytes) -> str | None:
        """Страница учебника в базу знаний: сбой диска не должен ломать проверку."""
        if self._kb_photos is None:
            return None
        try:
            return self._kb_photos.save(anonymize(user_id), image)
        except OSError:
            logger.exception("kb photo save failed")
            return None

    async def _check_task(self, user_id: int | None, task: VisionTask, index: int) -> CheckedTask:
        """`index` — номер задания в альбоме: модуль проверяет задания по одному и о своём
        месте в альбоме не знает, поэтому `Finding.task_index` проставляет бот."""
        subject_task = to_subject_task(task)
        [result] = await self._module.check([subject_task], [])
        findings = [f.model_copy(update={"task_index": index}) for f in result.findings]
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
        await self._record_findings(user_id, subject_task, findings)
        return CheckedTask(
            task=task,
            ref=ref,
            grade=grade,
            findings=findings,
            ref_status=payload["ref_status"],
        )

    async def _record_findings(
        self,
        user_id: int | None,
        task: SubjectTask,
        findings: list[Finding],
        *,
        subject: str = "math",
    ) -> None:
        for finding in findings:
            self._events.log(
                "finding_created",
                user_id=user_id,
                subject=subject,
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
                subject=subject,
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
        if clarification.kind == "word" and _finding(item, clarification) is None:
            # находка пропала/устарела между постановкой в очередь и вопросом (пересчёт другого
            # вопроса той же задачи — code review 17.09): молча снимаем вопрос, а не падаем
            # и не спрашиваем про случайную находку по тому же индексу
            await self._skip_clarification(chat_id, user_id, state, clarification)
            return
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
            reason=item.grade.uncertain_reason if item.grade is not None else None,
        )
        await self._max.send_message(chat_id, text, buttons=buttons)

    async def _skip_clarification(
        self, chat_id: int, user_id: int | None, state: ChatState, clarification: Clarification
    ) -> None:
        """Вопрос без живой находки снимается: сводка обещала его ребёнку — молчать нельзя."""
        item = state.tasks[clarification.task_index]
        rest = state.clarifications[1:]
        state = state.model_copy(
            update={"clarifications": rest, "phase": "clarifying" if rest else "review"}
        )
        await self._store.set(chat_id, state)
        self._events.log("clarification_skipped", user_id=user_id, kind=clarification.kind)
        await self._max.send_message(
            chat_id, f"{task_label(item.task)}: вопрос снят — оставлю «стоит перепроверить» 🤔"
        )
        if rest:
            await self._ask_clarification(chat_id, user_id, state)

    async def _word_image_token(
        self, state: ChatState, item: CheckedTask, clarification: Clarification
    ) -> str | None:
        """Кроп слова для вопроса «здесь написано …?»: любой сбой — вопрос уходит текстом."""
        try:
            finding = _finding(item, clarification)
            word = finding.word if finding is not None else None
            if word is None or word.box is None:
                return None
            if not 0 <= word.photo_index < len(state.photo_paths):
                # альбом в состоянии короче, чем ждёт находка (старое состояние Redis,
                # упавшее фото): это не сбой кропа — вопрос просто уходит текстом
                return None
            path = state.photo_paths[word.photo_index]
            image = self._photos.load(path) if self._photos is not None else None
            if image is None:
                return None
            # PIL блокирует поток: кроп уходит в отдельный, цикл событий бота остаётся свободным
            crop = await asyncio.to_thread(crop_word, image, word.box)
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
        after = updated or before
        self._events.log(
            "clarification_answered",
            user_id=user_id,
            user_initiated=True,
            kind=clarification.kind,
            understood=updated is not None,
            # у предмета без пересчёта вердикта нет — ответ виден в finding_confirmed
            verdict_before=before.grade.verdict if before.grade is not None else None,
            verdict_after=after.grade.verdict if after.grade is not None else None,
        )
        if updated is None:
            message = f"Хорошо, оставлю {_lower(task_label(before.task))} как есть 🤔"
            buttons = None
        else:
            tasks[clarification.task_index] = updated
            confirmed_finding = next(
                (f for f in updated.findings if f.id == clarification.finding_id), None
            )
            if clarification.kind == "word" and confirmed_finding is not None:
                # спецификация каркаса §8: доля «нет» — мера ложных срабатываний OCR по предмету
                self._events.log(
                    "finding_confirmed",
                    user_id=user_id,
                    user_initiated=True,
                    subject=state.subject,
                    kind=confirmed_finding.kind,
                    answer="yes" if confirmed_finding.confirmed else "no",
                )
            # отдельное событие: задание уже учтено в task_checked, в отчёте не дублируем.
            # Предмет без пересчёта (языки) сюда не попадает: пересчитывать нечего, а ответ
            # ребёнка уже записан в finding_confirmed
            if updated.grade is not None:
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
            session = await self._start_tutoring(user_id, index, item, state.subject)
        except ValueError as exc:
            # у языков разбирают подтверждённую ошибку: кнопка из старой сводки (находку сняли
            # ответом «нет») — это не сбой бота, так ребёнку и скажем. Текст в логе: сюда же
            # попал бы неожиданный ValueError модуля (в том числе ValidationError pydantic)
            logger.info("nothing to tutor: %s", exc)
            await self._max.send_message(chat_id, NOTHING_TO_TUTOR)
            return
        except Exception:
            logger.exception("tutoring start failed")
            await self._max.send_message(chat_id, RETRY)
            return
        try:
            reply, session = await tutor_reply(
                self._llm, session, "Помоги найти ошибку", model=self._settings.tutor_model
            )
        except Exception:
            logger.exception("tutor first reply failed")
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

    async def _start_tutoring(
        self, user_id: int | None, index: int, item: CheckedTask, subject: str
    ) -> TutorSession:
        module = self._module_for(subject)
        result = task_result_of(index, item)
        # subject_task: у языков в нём слова с координатами, у математики (и старых состояний
        # Redis) его нет — собираем из подписи задания, как раньше
        subject_task = item.subject_task or to_subject_task(item.task)
        session = await module.start_tutoring(result, subject_task, kb=self._kb)
        if session.error is not None:
            self._events.log(
                "error_classified",
                user_id=user_id,
                component="classifier",
                error_type=session.error.error_type,
            )
        if session.word is not None:
            # доля разборов без карточки правила — метрика классификатора орфограмм
            self._events.log(
                "orthogram_classified",
                user_id=user_id,
                subject=subject,
                rule_code=session.word.rule_code,
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


def _numbered_by_condition(
    task: SubjectTask, reference: Reference | None, conditions: dict[str, SubjectTask]
) -> SubjectTask:
    """Напечатанный номер упражнения надёжнее рукописного (как `attach_conditions` у математики):
    тетрадь без номера на странице подписывается номером своего упражнения из учебника."""
    if task.number_on_page or reference is None:
        return task
    condition = conditions.get(reference.task_number)
    if condition is None or not condition.number_on_page:
        return task
    return task.model_copy(update={"number": condition.number, "number_on_page": True})


def task_result_of(index: int, item: CheckedTask) -> TaskResult:
    """`TaskResult` для тьютора из уже посчитанного `CheckedTask`.

    Доверие эталону — по `item.ref_status`, а не по одному факту «эталон есть»:
    `checked.ref` из `bot/check.py` бывает не пуст только когда солвер сам себя проверил
    (`ref_status == "ok"`), но это поле не должно тихо подменяться в других сценариях.
    """
    if item.grade is None:
        # предмет без пересчёта: находки и эталон уже посчитал модуль, выводить нечего
        return TaskResult(
            task_index=index,
            findings=item.findings,
            reference=item.reference,
            payload=item.payload,
        )
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
