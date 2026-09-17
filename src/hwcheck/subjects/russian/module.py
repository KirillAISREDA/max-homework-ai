"""Русский язык через контракт `SubjectModule` (спецификация §6): «спиши, вставь буквы».

recognize — vision решает роль и читает печатный учебник, тетрадь читает OCR; resolve_reference —
эталон из базы знаний или словаря (LLM — только выбор из кандидатов), страница и ответ
сохраняются в базу; check — выравнивание слов, находки-кандидаты; start_tutoring — орфограмма →
карточка правила → сессия тьютора по слову.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hwcheck.bot.check import CheckModels
from hwcheck.ocr_client import OcrClient, OcrError
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, WordTutoring
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import (
    Finding,
    KnowledgeBase,
    NoTutorableFinding,
    Reference,
    SubjectPage,
    SubjectTask,
    TaskResult,
    Word,
)
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbTask
from hwcheck.subjects.russian.align import align
from hwcheck.subjects.russian.check import check_words
from hwcheck.subjects.russian.gaps import DerivedText, Dictionary, derive_text
from hwcheck.subjects.russian.recognize import (
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)
from hwcheck.subjects.russian.rules import classify_orthogram

logger = logging.getLogger(__name__)

NO_REFERENCE_DETAIL = "нет текста упражнения — пришли фото учебника"
OCR_FAILED_DETAIL = "не смог прочитать тетрадь — попробуй переснять"
NO_PAIR_DETAIL = "не понял, какое это упражнение — подпиши номер над работой"
# доля слов эталона, сошедшихся с тетрадью, с которой пара «тетрадь ↔ упражнение» считается той
# самой: ниже — это другое упражнение с той же страницы, и лучше попросить подписать номер
MIN_PAIR_SHARE = 0.4


@dataclass(frozen=True)
class _KbLookup:
    reference: Reference | None
    # задание уже есть в базе (хотя бы один ответ, включая отклонённые ревьюером) — не
    # пересоздавать ответ через `_save`, даже если действующего эталона сейчас нет
    known: bool


class RussianModule:
    code = "russian"

    def __init__(
        self,
        llm: VisionAndChatClient,
        models: CheckModels,
        *,
        ocr: OcrClient | None,
        dictionary: Dictionary,
    ) -> None:
        self._llm = llm
        self._models = models
        self._ocr = ocr
        self._dictionary = dictionary

    async def recognize(self, image: bytes) -> SubjectPage:
        page, usage = await recognize_page(self._llm, image, model=self._models.vision)
        if page.role == "textbook":
            return SubjectPage(
                subject=self.code, role="textbook", tasks=textbook_tasks(page, None), usage=usage
            )
        if page.role != "notebook":
            return SubjectPage(
                subject=self.code, role="unknown", tasks=[], comment=page.comment, usage=usage
            )
        words = await self._ocr_words(image)
        if words is None:
            return SubjectPage(
                subject=self.code, role="notebook", tasks=[], failure="ocr_failed", usage=usage
            )
        return SubjectPage(
            subject=self.code, role="notebook", tasks=[notebook_task(words)], usage=usage
        )

    async def _ocr_words(self, image: bytes) -> list[Word] | None:
        if self._ocr is None:
            return None
        try:
            return await self._ocr.recognize(image)
        except OcrError:
            logger.warning("ocr failed", exc_info=True)
            return None

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]:
        references = []
        for task in tasks:
            condition = task.condition.strip()
            if not condition:
                continue
            lookup = _KbLookup(None, False)
            read_ok = True
            if kb is not None:
                try:
                    lookup = await self._from_kb(task, condition, kb)
                except Exception:
                    # база недоступна — не роняем проверку, считаем страницу неизвестной и не
                    # пробуем писать в неё (запись почти наверняка тоже упадёт)
                    logger.warning("kb недоступна при чтении эталона", exc_info=True)
                    read_ok = False
            if lookup.reference is not None:
                references.append(lookup.reference)
                continue
            derived = await derive_text(
                condition, self._dictionary, self._llm, model=self._models.structure
            )
            reference = Reference(
                task_number=task.number,
                origin="derived",
                trust=derived.trust,
                payload=derived.model_dump(),
            )
            if kb is not None and read_ok and not lookup.known:
                try:
                    await self._save(task, condition, derived, kb)
                except Exception:
                    logger.warning("kb недоступна при сохранении эталона", exc_info=True)
            references.append(reference)
        return references

    async def _from_kb(self, task: SubjectTask, condition: str, kb: KnowledgeBase) -> _KbLookup:
        page = await kb.find_page(self.code, condition)
        if page is None or not page.tasks or page.tasks[0].id is None:
            return _KbLookup(None, False)
        raw_answers = await kb.answers_for(page.tasks[0].id)
        known = bool(raw_answers)
        answers = [a for a in raw_answers if a.status != "rejected"]
        if not answers:
            return _KbLookup(None, known)
        best = max(answers, key=lambda a: (a.trust == "verified", a.id or 0))
        derived = DerivedText.model_validate(best.answer)
        reference = Reference(
            task_number=task.number, origin="kb", trust=best.trust, payload=derived.model_dump()
        )
        return _KbLookup(reference, True)

    async def _save(
        self, task: SubjectTask, condition: str, derived: DerivedText, kb: KnowledgeBase
    ) -> None:
        page = KbPage(subject=self.code, text=condition, fingerprint="", photo_path=task.photo_path)
        kb_task = KbTask(number=task.number if task.number_on_page else None, condition=condition,
                         task_kind=task_kind(condition))  # fmt: skip
        saved = await kb.save_page(page, [kb_task])
        if saved.tasks and saved.tasks[0].id is not None:
            checked_by = "dictionary" if derived.trust == "verified" else None
            await kb.save_answer(
                KbAnswer(task_id=saved.tasks[0].id, answer=derived.model_dump(),
                         derived_by=derived.derived_by, checked_by=checked_by)
            )  # fmt: skip

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]:
        by_number = {r.task_number: r for r in references}
        results = []
        for index, task in enumerate(tasks):
            # по номеру — только если ребёнок подписал его над работой: присвоенные номера
            # («1», «2» по порядку упражнений) совпадают случайно и парой не являются
            reference = by_number.get(task.number) if task.number_on_page else None
            if reference is None and len(references) == 1 and len(tasks) == 1:
                reference = references[0]  # одна страница учебника и одна тетради — это пара
            if reference is None:
                reference = _pair_by_words(task, references)
            if reference is None:
                findings = [Finding(task_index=index, kind="uncertain", strength="candidate",
                                    detail=_no_pair_detail(task, references))]  # fmt: skip
                results.append(TaskResult(task_index=index, findings=findings))
                continue
            derived = DerivedText.model_validate(reference.payload)
            findings = check_words(index, task, derived)
            payload: dict[str, Any] = {
                "words": derived.words,
                "gap_indices": derived.gap_indices,
                "sentence": {f.actual: _sentence(task, f) for f in findings if f.actual},
            }
            results.append(
                TaskResult(
                    task_index=index, findings=findings, reference=reference, payload=payload
                )
            )
        return results

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession:
        # missing_word — тоже confirmed-способная находка (без actual), но разбор по слову
        # тьютору не построить без того, что ребёнок написал — только spelling/extra_word
        finding = next((f for f in result.findings if f.is_error and f.actual and f.expected), None)
        if finding is None or finding.actual is None or finding.expected is None:
            raise NoTutorableFinding("нет подтверждённой ошибки для разбора")
        sentence = result.payload.get("sentence", {}).get(finding.actual, " ".join(task.lines))
        rule_code = await classify_orthogram(
            self._llm, finding.actual, finding.expected, sentence, model=self._models.structure
        )
        rule = await kb.rule(rule_code) if kb is not None and rule_code else None
        word = WordTutoring(
            actual=finding.actual,
            expected=finding.expected,
            sentence=sentence,
            rule_code=rule_code,
            rule_title=rule.title if rule else None,
            rule_statement=rule.statement if rule else None,
            rule_example=rule.example if rule else None,
        )
        return TutorSession(
            task_text=f"Слово «{finding.actual}» в предложении: «{sentence}».",
            student_steps=[],
            student_answer=finding.actual,
            ref=RefSolution(steps=[], answer=finding.expected, units=None),
            expected=finding.expected,
            word=word,
        )


def _pair_by_words(task: SubjectTask, references: list[Reference]) -> Reference | None:
    """Пара для тетради без подписанного номера: упражнение, с которым сошлось больше всего слов.

    Номера нет (или он ни с чем не совпал), а на фото учебника упражнений несколько — раньше это
    означало «нет текста упражнения — пришли фото учебника» на уже присланном учебнике
    (ревью 17.09, I2). Порог `MIN_PAIR_SHARE` не даёт выдать за пару чужое упражнение.
    """
    actual = [word.text for word in task.words]
    if not actual:
        return None
    best: Reference | None = None
    best_share = 0.0
    for reference in references:
        words = _reference_words(reference)
        if not words:
            continue
        share = sum(1 for p in align(words, actual) if p.kind == "match") / len(words)
        if share > best_share:
            best, best_share = reference, share
    return best if best_share >= MIN_PAIR_SHARE else None


def _reference_words(reference: Reference) -> list[str]:
    try:
        return DerivedText.model_validate(reference.payload).words
    except Exception:
        # чужой/битый payload не должен ронять проверку: такой эталон просто не станет парой
        logger.warning("эталон без разобранного текста: %s", reference.task_number)
        return []


def _no_pair_detail(task: SubjectTask, references: list[Reference]) -> str:
    if not task.words:
        return OCR_FAILED_DETAIL
    return NO_REFERENCE_DETAIL if not references else NO_PAIR_DETAIL


def _sentence(task: SubjectTask, finding: Finding) -> str:
    """Строка тетради с этим словом — контекст для тьютора и классификатора.

    Восстанавливаем строку из слов тетради на той же строке, что и найденное слово (порядок —
    по x0): у задания слова могут быть без привязки к `task.lines` (например, в тестах модуля,
    где `SubjectTask` собран напрямую из `words`). Если слов на той строке нет — запасной путь
    через `task.lines` (реальная тетрадь из `notebook_task`, где `lines` всегда заполнены).
    """
    if finding.word is not None:
        line = finding.word.line or 0
        same_line = [w for w in task.words if (w.line or 0) == line]
        if same_line:
            ordered = sorted(same_line, key=lambda w: w.box.x0 if w.box else 0)
            return " ".join(w.text for w in ordered).strip(".,!?")
    if finding.line is not None and 1 <= finding.line <= len(task.lines):
        return task.lines[finding.line - 1].strip(".,!?")
    return " ".join(task.lines).strip(".,!?")
