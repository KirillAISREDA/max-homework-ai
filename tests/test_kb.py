"""Контракт базы знаний (спецификация каркаса §5) — одни тесты для памяти и PostgreSQL."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from hwcheck.db.kb import PgKnowledgeBase, fingerprint
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.db.pool import create_pool
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbRule, KbTask

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
KnowledgeBaseImpl = PgKnowledgeBase | InMemoryKnowledgeBase


@pytest.fixture(params=["memory", "postgres"])
async def kb(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeBaseImpl]:
    if request.param == "memory":
        yield InMemoryKnowledgeBase()
        return
    if TEST_DATABASE_URL is None:
        if "CI" in os.environ:
            pytest.fail("в CI нужен TEST_DATABASE_URL")
        pytest.skip("нужен TEST_DATABASE_URL (PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await create_pool(TEST_DATABASE_URL, server_settings={"search_path": schema})
        try:
            yield PgKnowledgeBase(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


def page(text: str = "Упр. 34. На п_ляне р_сли б_рёзы.") -> KbPage:
    return KbPage(subject="russian", grade=2, fingerprint=fingerprint(text), text=text)


def test_fingerprint_ignores_layout_and_yo() -> None:
    assert fingerprint("На  поляне\nрОсли берёзы.") == fingerprint("на поляне росли березы")
    assert fingerprint("803 + 169") != fingerprint("803 + 196")


async def test_page_saved_once_and_found_by_text(kb: KnowledgeBaseImpl) -> None:
    tasks = [KbTask(number="34", condition="На п_ляне р_сли б_рёзы.", task_kind="fill_letters")]
    saved = await kb.save_page(page(), tasks)
    assert saved.id is not None and [t.id for t in saved.tasks] != [None]
    again = await kb.save_page(page(), tasks)  # то же фото от другого ученика
    assert again.id == saved.id and len(again.tasks) == 1
    found = await kb.find_page("russian", "  на поляне  росли берёзы. упр. 34")
    assert found is None  # другой порядок слов — другая страница
    found = await kb.find_page("russian", "Упр. 34. На п_ляне р_сли б_рёзы.")
    assert found is not None and found.tasks[0].number == "34"
    assert await kb.find_page("english", "Упр. 34. На п_ляне р_сли б_рёзы.") is None


async def test_answers_trust_and_review_queue(kb: KnowledgeBaseImpl) -> None:
    saved = await kb.save_page(
        page(), [KbTask(number="34", condition="x", task_kind="fill_letters")]
    )
    task_id = saved.tasks[0].id
    assert task_id is not None
    by_dict = await kb.save_answer(
        KbAnswer(
            task_id=task_id,
            answer={"text": "поляне"},
            derived_by="dictionary",
            checked_by="dictionary",
        )  # fmt: skip
    )
    by_llm = await kb.save_answer(
        KbAnswer(task_id=task_id, answer={"text": "росли"}, derived_by="llm:GigaChat-2-Pro@v1")
    )
    assert by_dict.trust == "verified" and by_llm.trust == "unverified"
    assert [a.id for a in await kb.answers_for(task_id)] == [by_dict.id, by_llm.id]
    queue = await kb.unverified_answers("russian", limit=10)
    assert [(t.number, a.id) for t, a in queue] == [("34", by_llm.id)]
    assert by_llm.id is not None
    await kb.set_answer_status(by_llm.id, "verified", checked_by="manual")
    [_, reviewed] = await kb.answers_for(task_id)
    assert reviewed.status == "verified" and reviewed.reviewed_at is not None
    assert await kb.unverified_answers("russian", limit=10) == []


async def test_rules_and_words(kb: KnowledgeBaseImpl) -> None:
    rule = KbRule(
        code="ru.orth.unstressed_vowel", subject="russian", grade_from=2,
        title="Безударная гласная в корне", statement="Подбери проверочное слово.",
        example="п_ляне — по́ле → поляне", finding_kinds=["spelling"],
    )  # fmt: skip
    await kb.add_rule(rule)
    await kb.add_rule(rule)  # повтор не падает и не дублирует
    assert await kb.rule("ru.orth.unstressed_vowel") == rule
    assert await kb.rule("nope") is None
    await kb.add_words(
        "english", "irregular_verbs", {"go": {"past": "went"}, "see": {"past": "saw"}}
    )
    await kb.add_words("english", "irregular_verbs", {"go": {"past": "went"}})
    assert await kb.words("english", "irregular_verbs") == {"go", "see"}
    assert await kb.words("english", "grade_list:3") == set()


async def test_page_fingerprint_comes_from_text(kb: KnowledgeBaseImpl) -> None:
    """Отпечаток считается по тексту при сохранении: переданный (чужой/битый) не должен
    прятать страницу от поиска (ревью 17.09, F3)."""
    text = "Упр. 7. Спиши, вставь буквы."
    saved = await kb.save_page(KbPage(subject="russian", fingerprint="wrong", text=text), [])

    assert saved.fingerprint == fingerprint(text)
    found = await kb.find_page("russian", text)
    assert found is not None and found.id == saved.id
