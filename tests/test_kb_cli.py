"""Консольная проверка базы знаний: очередь непроверенных ответов, загрузка словарей."""

from pathlib import Path

from hwcheck.db.kb import fingerprint
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.kb_cli import load_words, review
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbTask


async def seed(kb: InMemoryKnowledgeBase) -> tuple[int, list[int]]:
    page = KbPage(subject="russian", grade=2, fingerprint=fingerprint("x"), text="x")
    saved = await kb.save_page(
        page, [KbTask(number="34", condition="п_ляне", task_kind="fill_letters")]
    )
    task_id = saved.tasks[0].id
    assert task_id is not None
    ids = []
    for text in ("поляне", "пыляне"):
        answer = await kb.save_answer(
            KbAnswer(task_id=task_id, answer={"text": text}, derived_by="llm:GigaChat-2-Pro@v1")
        )
        assert answer.id is not None
        ids.append(answer.id)
    return task_id, ids


async def test_review_applies_answers_and_stops_on_quit() -> None:
    kb = InMemoryKnowledgeBase()
    task_id, ids = await seed(kb)
    answers = iter(["y", "n"])
    shown: list[str] = []
    done = await review(kb, "russian", limit=10, read=lambda _: next(answers), write=shown.append)
    assert done == 2
    assert "п_ляне" in shown[0] and "поляне" in shown[0]
    statuses = {a.id: (a.status, a.checked_by) for a in await kb.answers_for(task_id)}
    assert statuses[ids[0]] == ("verified", "manual") and statuses[ids[1]] == ("rejected", "manual")
    assert await review(kb, "russian", limit=10, read=lambda _: "q", write=shown.append) == 0


async def test_review_edit_creates_verified_answer() -> None:
    kb = InMemoryKnowledgeBase()
    task_id, [first, _] = await seed(kb)
    answers = iter(["e", "поляне", "q"])
    await review(kb, "russian", limit=10, read=lambda _: next(answers), write=lambda _: None)
    all_answers = await kb.answers_for(task_id)
    assert [a.status for a in all_answers if a.id == first] == ["rejected"]
    edited = all_answers[-1]
    assert (edited.answer, edited.status, edited.derived_by) == (
        {"text": "поляне"},
        "verified",
        "manual",
    )


async def test_load_words(tmp_path: Path) -> None:
    kb = InMemoryKnowledgeBase()
    plain = tmp_path / "words.txt"
    plain.write_text("собака\nкорова\n\nсобака\n", encoding="utf-8")
    assert await load_words(kb, "russian", "grade_list:2", plain) == 2
    assert await kb.words("russian", "grade_list:2") == {"собака", "корова"}
    verbs = tmp_path / "verbs.json"
    verbs.write_text('{"go": {"past": "went"}}', encoding="utf-8")
    assert await load_words(kb, "english", "irregular_verbs", verbs) == 1
