"""База знаний в памяти — для тестов модулей и бота; контракт тот же, что у PgKnowledgeBase."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hwcheck.db.kb import fingerprint
from hwcheck.subjects.kb_models import AnswerStatus, KbAnswer, KbPage, KbRule, KbTask


class InMemoryKnowledgeBase:
    def __init__(self) -> None:
        self._pages: dict[int, KbPage] = {}
        self._answers: dict[int, KbAnswer] = {}
        self._rules: dict[str, KbRule] = {}
        self._words: dict[tuple[str, str], dict[str, dict[str, Any] | None]] = {}
        self._last_id = 0

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    async def find_page(self, subject: str, text: str) -> KbPage | None:
        key = fingerprint(text)
        return next(
            (p for p in self._pages.values() if p.subject == subject and p.fingerprint == key),
            None,
        )

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage:
        existing = await self.find_page(page.subject, page.text)
        if existing is not None:
            return existing
        saved_tasks = [t.model_copy(update={"id": self._next_id()}) for t in tasks]
        # отпечаток — по тексту, как в PgKnowledgeBase: переданный не сверяем и не храним
        saved = page.model_copy(
            update={
                "id": self._next_id(),
                "fingerprint": fingerprint(page.text),
                "tasks": saved_tasks,
            }
        )
        assert saved.id is not None
        self._pages[saved.id] = saved
        return saved

    async def answers_for(self, task_id: int) -> list[KbAnswer]:
        return sorted(
            (a for a in self._answers.values() if a.task_id == task_id), key=lambda a: a.id or 0
        )

    async def save_answer(self, answer: KbAnswer) -> KbAnswer:
        # детерминированные ответы (словарь/правило) не требуют ручной проверки — статус в базе
        # сразу «verified», иначе они попали бы в очередь unverified_answers наравне со спорными.
        status = "verified" if answer.trust == "verified" else answer.status
        saved = answer.model_copy(update={"id": self._next_id(), "status": status})
        assert saved.id is not None
        self._answers[saved.id] = saved
        return saved

    async def unverified_answers(self, subject: str, limit: int) -> list[tuple[KbTask, KbAnswer]]:
        tasks = {t.id: t for p in self._pages.values() if p.subject == subject for t in p.tasks}
        queue = [
            (tasks[a.task_id], a)
            for a in sorted(self._answers.values(), key=lambda a: a.id or 0)
            if a.status == "unverified" and a.task_id in tasks
        ]
        return queue[:limit]

    async def set_answer_status(
        self, answer_id: int, status: AnswerStatus, checked_by: str | None
    ) -> None:
        answer = self._answers[answer_id]
        self._answers[answer_id] = answer.model_copy(
            update={"status": status, "checked_by": checked_by, "reviewed_at": datetime.now(UTC)}
        )

    async def rule(self, code: str) -> KbRule | None:
        return self._rules.get(code)

    async def add_rule(self, rule: KbRule) -> None:
        self._rules[rule.code] = rule

    async def words(self, subject: str, source: str) -> set[str]:
        return set(self._words.get((subject, source), {}))

    async def add_words(
        self, subject: str, source: str, words: dict[str, dict[str, Any] | None]
    ) -> None:
        self._words.setdefault((subject, source), {}).update(words)
