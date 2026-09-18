"""Распознавание страниц для русского языка (спецификация §6).

Учебник — печатный текст, его читает GigaChat-vision (с пропусками «как напечатано»). Тетрадь —
рукопись ученика, её vision НЕ читает (исследование 13.09: скрывает 78–80 % ошибок), только
определяет роль; слова «как написано» даёт OCR-сервис (`OcrClient`).
"""

from __future__ import annotations

import re
from statistics import median
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from hwcheck.llm.base import VisionClient, extract_json
from hwcheck.pipeline.normalize import normalize_image, rotate_image
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import SubjectTask, Usage, Word
from hwcheck.subjects.russian.align import normalize

# без 180°: печатную страницу вверх ногами модель не прочитает и перевёрнутой — типографику она
# узнаёт по общему виду текста, а не по строкам, доворачивать лишний раз незачем — экономим вызов
ORIENTATIONS = (0, 270, 90)
_NUMBER = re.compile(r"(?:упр\w*\.?|№|n)\s*(\d{1,4})", re.IGNORECASE)
# «голый» номер — только когда строка ЦЕЛИКОМ им и является (иначе дата «17 сентября 2026»,
# страница «стр. 12» и год «в 1945 году» ловились бы как номер упражнения); 3 цифры — не 4, чтобы
# год «2026» отдельной строкой не совпал
_BARE_NUMBER = re.compile(r"^(\d{1,3})\.?$")
FILLED_BY_HAND_COMMENT = "страница уже заполнена от руки — нужна чистая страница учебника"
# дефис переноса как его отдаёт OCR: обычный, неразрывный (U+2010) и короткое тире (U+2013)
HYPHENS = "-‐–"
MIN_HYPHEN_STEM_LETTERS = 2
# доля медианной высоты слова: дальше по вертикали от медианы строки — уже следующая строка
LINE_GAP_RATIO = 0.6


class RuExercise(BaseModel):
    number: str | None = None
    instruction: str = ""
    text: str


class RuPage(BaseModel):
    role: Literal["textbook", "notebook", "unknown"]
    exercises: list[RuExercise] = Field(default_factory=list)
    comment: str | None = None
    # на странице есть рукописные записи ученика (заполненные пропуски, ответы): печатной
    # страницей учебника такая рабочая тетрадь быть перестаёт — поле последним, чтобы старые
    # ответы модели без него валидировались
    handwritten: bool = False


def _hide_handwriting(page: RuPage) -> RuPage:
    """Vision не должен пересказывать рукопись ученика (исследование 13.09: прячет 78–80 % её
    ошибок) — если модель всё же вернула exercises не для "textbook", подчищаем сами, а не
    полагаемся только на промпт.
    """
    if page.role != "textbook":
        return page.model_copy(update={"exercises": []})
    return page


async def recognize_page(
    client: VisionClient, image: bytes, *, model: str, prompt_version: str = "v1"
) -> tuple[RuPage, Usage, int]:
    """Роль страницы, печатные упражнения и ориентация, в которой страница узналась.

    На «unknown» и на "textbook" без упражнений пробуем следующую ориентацию — не как в
    `pipeline/vision.py` (математика): без 180° (см. комментарий у `ORIENTATIONS`) и без выбора
    «лучшей» по числу заданий страницы — только первая, где нашлись упражнения (или "notebook").

    Градусы отдаём наверх: рукопись читает OCR, и читать он обязан тот же кадр, в котором vision
    узнал страницу — иначе снятая боком тетрадь превращается в мусор (живой прогон 18.09, ru-2).
    """
    prompt = load_prompt("ru_page", prompt_version)
    normalized = normalize_image(image)
    usage = Usage()
    page = RuPage(role="unknown")
    fallback: tuple[RuPage, int] | None = None  # первая распознанная не-"unknown" страница — если
    # так и не найдём упражнений ни в одной ориентации, вернём её, а не "unknown" последней попытки
    for degrees in ORIENTATIONS:
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        result = await client.analyze_image(data, prompt=prompt, model=model, filename="page.jpg")
        usage.calls += 1
        usage.tokens += result.tokens_in + result.tokens_out
        try:
            page = RuPage.model_validate_json(extract_json(result.content))
        except ValidationError:
            page = RuPage(role="unknown", comment="ответ модели не разобран")
        page = _hide_handwriting(page)
        if page.role == "textbook" and page.handwritten:
            # заполненная от руки рабочая тетрадь: vision читает её вместе с рукописью ученика,
            # и эталон совпал бы с тем, что ребёнок написал — проверка всегда говорила бы
            # «верно», а текст и фото ушли бы в базу знаний (финальное ревью 17.09, I5).
            # Доворачивать незачем — страница не станет чистой от поворота
            filled = RuPage(role="unknown", exercises=[], comment=FILLED_BY_HAND_COMMENT)
            return filled, usage, degrees
        if page.role == "notebook" or page.exercises:
            return page, usage, degrees
        if fallback is None and page.role != "unknown":
            fallback = (page, degrees)
    if fallback is not None:
        return fallback[0], usage, fallback[1]
    # страницу не узнали ни в одной ориентации: поворачивать нечего и незачем
    return page, usage, 0


def task_kind(condition: str) -> str:
    if "_" in condition:
        return "fill_letters"
    if "(" in condition:
        return "expand_brackets"
    return "copy"


def textbook_tasks(page: RuPage, photo_path: str | None) -> list[SubjectTask]:
    """Номер — печатный, если есть; иначе наименьшее свободное число (не занятое печатными
    номерами этой страницы и уже розданными автономерами — коллизий не бывает, спецификация §6).
    """
    exercises = [exercise for exercise in page.exercises if exercise.text.strip()]
    used = {int(e.number) for e in exercises if e.number is not None and e.number.isdigit()}
    tasks = []
    for exercise in exercises:
        if exercise.number is not None:
            number, number_on_page = exercise.number, True
        else:
            candidate = 1
            while candidate in used:
                candidate += 1
            used.add(candidate)
            number, number_on_page = str(candidate), False
        tasks.append(
            SubjectTask(
                number=number,
                number_on_page=number_on_page,
                condition=exercise.text.strip(),
                photo_path=photo_path,
            )
        )
    return tasks


def notebook_task(words: list[Word]) -> SubjectTask:
    """Страница тетради — одно задание: строки по геометрии рамок, слова слева направо."""
    ordered = sorted(_reading_order(words), key=lambda w: (w.line or 0, w.box.x0 if w.box else 0))
    lines: dict[int, list[str]] = {}
    for word in ordered:
        lines.setdefault(word.line or 0, []).append(word.text)
    texts = [" ".join(parts) for _, parts in sorted(lines.items())]
    number = header_number(texts[:2])
    return SubjectTask(
        number=number or "1",
        number_on_page=number is not None,
        lines=texts,
        words=ordered,
        confidence=min((w.confidence for w in words if w.confidence is not None), default=1.0),
    )


def merge_hyphenation(words: list[Word], reference: set[str] | None = None) -> list[Word]:
    """Слово, перенесённое на следующую строку, — одно слово: «сред-» + «них» = «сред-них».

    Слова приходят в порядке чтения (так их отдаёт `notebook_task`), поэтому «конец строки» —
    это смена `line` у соседней пары. Без склейки обе половины уходили в находки: «сред-» как
    описка, «них» как лишнее слово (живой прогон 18.09, ru-1 — 25 находок на изложении).

    Дефис в склеенном слове остаётся, а рамка берётся у ЛЕВОЙ половины (ревью ветки): вопрос
    «здесь написано «сде-делал»?» называет то, что ребёнок и правда написал, а кроп показывает
    место на странице, а не две строки целиком (объединение рамок давало 793×118 на ru-6).
    При сверке дефис не значим — его снимает `normalize`.

    `reference` — нормализованные слова эталона. Тире в конце строки («до дома — / уставшие»)
    выглядит так же, как перенос, поэтому склеиваем, только если склеенное слово есть в эталоне
    ИЛИ ни одной половины в эталоне нет (эталона не дали — склейка вероятнее).
    """
    plan = _hyphenation_plan(words, reference)
    merged: list[Word] = []
    consumed: set[int] = set()
    for index, word in enumerate(words):
        if index in consumed:
            continue
        right_index = plan.get(index)
        if right_index is None:
            merged.append(word)
            continue
        consumed.add(right_index)
        stem = _hyphen_stem(word.text) or word.text
        merged.append(_join_words(word, words[right_index], stem))
    return merged


def _hyphenation_plan(words: list[Word], reference: set[str] | None) -> dict[int, int]:
    """Пары «конец строки → начало следующей», которые надо склеить: левый индекс → правый.

    Слова без номера строки (`line is None` — поля тетради, номера на полях: их OCR в строку не
    собрал) в строки не входят и последним словом строки не считаются: иначе цифра с поля
    вклинивалась бы между половинами перенесённого слова (живой прогон 18.09, «раз-/вести»).
    """
    lines: dict[int, list[int]] = {}
    for index, word in enumerate(words):
        if word.line is not None:
            lines.setdefault(word.line, []).append(index)
    plan: dict[int, int] = {}
    keys = sorted(lines)
    for current, following in zip(keys, keys[1:], strict=False):
        left_index, right_index = lines[current][-1], lines[following][0]
        stem = _hyphen_stem(words[left_index].text)
        if stem is not None and _is_hyphenation(stem, words[right_index].text, reference):
            plan[left_index] = right_index
    return plan


def _hyphen_stem(text: str) -> str | None:
    """Начало перенесённого слова без дефиса; `None` — переносом это быть не может."""
    if not text or text[-1] not in HYPHENS:
        return None
    stem = text.rstrip(HYPHENS)
    # «а -» в конце строки — тире, а не перенос: слово переносят минимум с двух букв
    if sum(1 for char in stem if char.isalpha()) < MIN_HYPHEN_STEM_LETTERS:
        return None
    return stem


def _is_hyphenation(stem: str, right: str, reference: set[str] | None) -> bool:
    if reference is None or normalize(stem + right) in reference:
        return True
    return normalize(stem) not in reference and normalize(right) not in reference


def _join_words(left: Word, right: Word, stem: str) -> Word:
    confidences = [c for c in (left.confidence, right.confidence) if c is not None]
    return Word(
        text=f"{stem}-{right.text}",
        box=left.box,
        confidence=min(confidences) if confidences else None,
        line=left.line,
        photo_index=left.photo_index,
    )


def _reading_order(words: list[Word]) -> list[Word]:
    """Свой порядок строк по рамкам слов (исследование 13.09: «порядок строк нужно делать своим»).

    Движок собирает строки по своему разумению и на наклонной странице путает соседние: в ru-6
    (живой прогон 18.09) одна строка «Белка спрятала орехи в» разошлась на две и встала задом
    наперёд, а половинки переноса «прово-/дить» оказались в разных кусках.

    Куски движка порядком не считаем: строки идут по вертикали (медиана центров слов), а внутри
    строки слова — слева направо, с нашей нумерацией. Куски, чьи медианы расходятся меньше чем на
    `LINE_GAP_RATIO` медианной высоты слова И которые не перекрываются по горизонтали, — одна
    строка, разорванная движком: их склеиваем. Перекрытие по горизонтали означает, что это две
    настоящие строки одна над другой: половинки разорванной строки стоят рядом, а не друг на друге.

    Строим НАД кусками движка, а не по словам с нуля: на живых фото строка «уезжает» вниз к
    правому краю на целую высоту строки (ru-1, ru-3 18.09), и порогом по центру слова строки
    рассыпаются — а куски движка на тех же фото собраны верно. Здесь мы чиним ровно то, что он
    ломает: разорванную строку и её порядок.

    Запасной путь — строки движка как есть: без рамки хотя бы у одного слова геометрии нет.
    Слова, для которых движок строки не нашёл (`line is None`), в строки не собираем: это
    пометки на полях, и в тексте им не место (см. `check._body_words`).
    """
    # рамки нужны только у слов строк: пометка на полях без рамки (её движок в строку и не
    # собрал) не должна отключать свой порядок строк для всей страницы
    lined = [word for word in words if word.line is not None]
    if not lined or any(word.box is None for word in lined):
        return words
    heights = sorted(_height(word) for word in lined)
    limit = LINE_GAP_RATIO * heights[len(heights) // 2]
    chunks: dict[int, list[Word]] = {}
    for word in lined:
        chunks.setdefault(word.line or 0, []).append(word)
    rows: list[list[Word]] = []
    for chunk in sorted(chunks.values(), key=_row_center):
        near = rows and _row_center(chunk) - _row_center(rows[-1]) <= limit
        if near and not _overlap_in_x(rows[-1], chunk):
            rows[-1] += chunk
        else:
            rows.append(list(chunk))
    ordered = [word for word in words if word.line is None]
    for index, row in enumerate(rows):
        ordered += [w.model_copy(update={"line": index}) for w in sorted(row, key=_x0)]
    return ordered


def _row_center(row: list[Word]) -> float:
    return median(_y_center(word) for word in row)


def _overlap_in_x(left: list[Word], right: list[Word]) -> bool:
    """Пересекаются ли куски по горизонтали — тогда это две строки, а не половинки одной."""
    left_end, right_end = max(_x1(w) for w in left), max(_x1(w) for w in right)
    left_start, right_start = min(_x0(w) for w in left), min(_x0(w) for w in right)
    return left_end > right_start and right_end > left_start


def _y_center(word: Word) -> float:
    return (word.box.y0 + word.box.y1) / 2 if word.box is not None else 0.0


def _height(word: Word) -> int:
    return word.box.y1 - word.box.y0 if word.box is not None else 0


def _x0(word: Word) -> int:
    return word.box.x0 if word.box is not None else 0


def _x1(word: Word) -> int:
    return word.box.x1 if word.box is not None else 0


def header_number(lines: list[str]) -> str | None:
    """Номер упражнения в первых строках тетради: «Упр. 245», «№ 245» — где угодно в строке;
    «голый» номер («245.») — только если им является строка целиком (иначе дата, номер страницы
    учебника и год ловились бы как номер упражнения, ревью 17.09)."""
    for line in lines:
        match = _NUMBER.search(line)
        if match:
            return match.group(1)
    for line in lines:
        match = _BARE_NUMBER.match(line.strip())
        if match:
            return match.group(1)
    return None
