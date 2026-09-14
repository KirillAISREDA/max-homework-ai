"""Страница-продолжение в альбоме (живой альбом 14.09).

Второе фото — «Ответ: было — 35 луковиц» к №57 и край соседней страницы. Номера на странице нет,
структуризатор назвал задание «№1», и бот проверил его отдельно. Единственное задание без номера
после задания с номером, у которого нет ответа, — продолжение этого задания.
"""

from hwcheck.bot.check import RecognizedPhoto, split_pages
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.vision import RecognizedPage


def photo(role: str, *tasks: VisionTask) -> RecognizedPhoto:
    page = VisionPage(tasks=list(tasks), page_ok=True)
    rec = RecognizedPage(page, 0, 1, 0, 0, 0.0, "")
    return RecognizedPhoto(page=page, role=role, rec=rec)  # type: ignore[arg-type]


N55 = VisionTask(number=55, task_text="", student_solution_steps=["748 : 2 = 374"], confidence=0.9)
N57 = VisionTask(
    number=57,
    task_text="",
    student_solution_steps=["20 : 4 = 5", "20 + 5 + 10 = 35"],
    confidence=0.9,
)
ANSWER_PAGE = VisionTask(
    number=1,
    number_on_page=False,
    task_text="",
    student_solution_steps=["756 : 6 = 126"],
    student_answer="было 35 луковиц",
    confidence=0.7,
)


def test_unnumbered_page_continues_previous_task_without_answer() -> None:
    album = split_pages([photo("notebook", N55, N57), photo("notebook", ANSWER_PAGE)], [])
    assert [t.number for t in album.notebook] == [55, 57]
    merged = album.notebook[1]
    assert merged.student_answer == "было 35 луковиц"
    assert merged.student_solution_steps == ["20 : 4 = 5", "20 + 5 + 10 = 35", "756 : 6 = 126"]
    assert merged.confidence == 0.7
    assert merged.number_on_page


def test_continuation_sent_before_its_page_is_attached_too() -> None:
    """Порядок живого альбома 14.09: учебник, страница с ответом, страница с №55 и №57."""
    condition = VisionTask(number=57, task_text="Бабушка посадила 20 луковиц", confidence=1)
    photos = [
        photo("textbook", condition),
        photo("notebook", ANSWER_PAGE),
        photo("notebook", N55, N57),
    ]
    album = split_pages(photos, [])
    assert [t.number for t in album.notebook] == [55, 57]
    assert album.notebook[1].student_answer == "было 35 луковиц"


def test_textbook_between_pages_does_not_break_continuation() -> None:
    condition = VisionTask(number=57, task_text="Бабушка посадила 20 луковиц", confidence=1)
    photos = [photo("notebook", N57), photo("textbook", condition), photo("notebook", ANSWER_PAGE)]
    assert [t.number for t in split_pages(photos, []).notebook] == [57]


def test_not_a_continuation() -> None:
    answered = N57.model_copy(update={"student_answer": "35 луковиц"})
    unnumbered = N57.model_copy(update={"number": 1, "number_on_page": False})
    second = ANSWER_PAGE.model_copy(update={"number": 2})
    # повторный прогон живого фото: вместо ответа выдуманные дроби — к №57 не приклеиваем
    no_answer = ANSWER_PAGE.model_copy(
        update={"student_solution_steps": ["7/3 * 3/2 = 7/2"], "student_answer": None}
    )
    cases = [
        [photo("notebook", N57), photo("notebook", no_answer)],
        [photo("notebook", ANSWER_PAGE)],  # первая страница — задание само по себе
        [photo("notebook", answered), photo("notebook", ANSWER_PAGE)],  # у №57 уже есть ответ
        [photo("notebook", N57), photo("notebook", ANSWER_PAGE, second)],  # два задания
        [photo("notebook", unnumbered), photo("notebook", ANSWER_PAGE)],  # номеров нет нигде
    ]
    for photos in cases:
        expected = sum(len(p.page.tasks) for p in photos if p.page is not None)
        assert len(split_pages(photos, []).notebook) == expected
