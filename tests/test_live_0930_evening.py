"""Живая проверка 30.09, 20:33 МСК: №2.184, 2.189, 2.191 на трёх страницах тетради и учебник.

Бот ответил семью строками: «Задание 1», «Задание 2», «№1791» дважды, «Задание 119»; верные
подстановки — «не смог разобрать решение». Причины: модель кладёт две строки тетради в один шаг
(«c = 720, d = 382» и «(720 + 382) − 763 = 339» через перевод строки), эталон нескольких
подстановок — список «[339, 7254]», а задание, продолженное на другой странице или разбитое по
пунктам, становится несколькими заданиями.
"""

from hwcheck.bot.check import (
    RecognizedPhoto,
    _split_task_columns,
    split_pages,
    validator_only_grade,
)
from hwcheck.bot.fsm import CheckedTask
from hwcheck.bot.pages import attach_conditions, mark_written_numbers
from hwcheck.bot.summary import task_line
from hwcheck.pipeline.grade import grade, is_multipart
from hwcheck.pipeline.schemas import VisionPage, VisionTask
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.vision import RecognizedPage


def _task(number: int, text: str, steps: list[str], answer: str | None = None) -> VisionTask:
    return VisionTask(
        number=number,
        task_text=text,
        student_solution_steps=steps,
        student_answer=answer,
        confidence=0.9,
    )


def _photo(tasks: list[VisionTask], transcript: str, role: str = "notebook") -> RecognizedPhoto:
    page = VisionPage(tasks=tasks, page_ok=True)
    page = mark_written_numbers(_split_task_columns(page), transcript)
    rec = RecognizedPage(
        page=page, orientation=0, attempts=0, tokens_in=0, tokens_out=0, latency_s=0.0,
        raw=transcript,
    )  # fmt: skip
    return RecognizedPhoto(page=page, role=role, rec=rec)  # type: ignore[arg-type]


# страницы тетради так, как их разобрала модель (состояние чата в проде)
PAGE_1 = _photo(
    [
        _task(2184, "", ["(106+68)-(23+59)", "(c-86)-111", "(x-23)-(y-45)", "(273+m)-(104-n)"]),
        _task(
            2189,
            "",
            ["n+6775 при n=657, 4315", "n=657", "657+6775=7432", "n=4315", "4315+6775=11090"],
            answer="<неразборчиво>",
        ),
    ],
    "Домашняя работа\n№ 2.184\nа) (106+68)-(23+59)\nб) (c-86)-111\n№ 2.189\nn+6775 при n=657, 4315",
)
PAGE_2 = _photo(
    [
        _task(1, "б) 41942 - z при z = 39761, 21042", ["41042 - 39761 = 1821"]),
        _task(
            2,
            "в) (c + d) - 763 при c = 720,\nd = 382; c = 7112, d = 905",
            [
                "c = 720, d = 382\n(720 + 382) - 763 = 339",
                "c = 7112, d = 905\n(7112 + 905) - 763 = 7254",
            ],
        ),
        _task(
            1791,
            "а) 255 - c + 245 при c =\n184; 123.",
            ["c = 184\n255 - 184 + 245 = 316", "c = 123\n255 - 123 + 245 = 377"],
        ),
        _task(1791, "б) 506 - s - 246 при s = 95\ns = 260", ["506 - 95 - 246 = 260"]),
    ],
    "б) 41942 - z при z = 39761, 21042\n41042 - 39761 = 1821\nв) (c + d) - 763\n"
    "№ 1.791\nа) 255 - c",
)
PAGE_3 = _photo(
    [_task(119, "", ["506 - 260 - 246 = 0"])], "№1.191(продолжение)\ns = 260\n506 - 260 - 246 = 0"
)
TEXTBOOK = _photo(
    [
        _task(2184, "Запишите разность:", []),
        _task(2189, "Найдите значение выражения:", []),
        _task(2191, "Упростите выражение и найдите его значение:", []),
    ],
    "2.184 Запишите разность:\n2.189 Найдите значение выражения:\n2.191 Упростите выражение",
    role="textbook",
)


def _checked() -> list[CheckedTask]:
    album = split_pages([PAGE_1, PAGE_2, PAGE_3, TEXTBOOK], [])
    tasks = attach_conditions(album.notebook, album.textbook)
    return [
        CheckedTask(
            task=t,
            ref=None,
            grade=validator_only_grade(t.student_solution_steps, condition=t.task_text or None),
        )
        for t in tasks
    ]


def test_two_lines_in_one_step_are_split() -> None:
    [_, part, _, _] = PAGE_2.page.tasks  # type: ignore[union-attr]
    assert part.student_solution_steps[:2] == ["c = 720, d = 382", "(720 + 382) - 763 = 339"]


def test_parts_of_one_task_become_one_task() -> None:
    tasks = [t.task for t in _checked()]
    assert [(t.number, t.number_label) for t in tasks] == [
        (2184, "2.184"),
        (2189, "2.189"),
        (1791, "1.791"),
    ]
    assert tasks[1].student_solution_steps[0] == "а) n+6775 при n=657, 4315"
    assert "б) 41042 - 39761 = 1821" in tasks[1].student_solution_steps
    assert tasks[2].student_solution_steps[-1] == "506 - 260 - 246 = 0"  # страница «(продолжение)»


def test_summary_of_the_live_check() -> None:
    lines = [task_line(i, item)[0] for i, item in enumerate(_checked())]
    assert lines == [
        "№2.184 — вижу выражения без вычислений: такие задания пока не проверяю 🤔",
        "№2.189 — есть ошибка в пункте б) ❌ Верно: а), в).",
        "№1.791 — есть ошибка в пункте б) ❌ Верно: а).",
    ]


def test_several_substitutions_are_graded_by_lines() -> None:
    condition = "в) (c + d) - 763 при c = 720, d = 382; c = 7112, d = 905"
    steps = [
        "c = 720, d = 382",
        "(720 + 382) - 763 = 339",
        "c = 7112, d = 905",
        "(7112 + 905) - 763 = 7254",
    ]
    listed = RefSolution(steps=[], answer="[339, 7254]")
    assert is_multipart(condition, steps)
    assert grade(steps, None, listed, condition=condition).verdict == "correct"
    # эталон списком, а в условии подстановки через запятую — тоже по строкам
    single = "б) 41942 - z при z = 39761, 21042"
    wrong = grade(["41042 - 39761 = 1821"], None, RefSolution(steps=[], answer="[2181, 20900]"),
                  condition=single)  # fmt: skip
    assert (wrong.verdict, wrong.first_error_line) == ("wrong", 1)
