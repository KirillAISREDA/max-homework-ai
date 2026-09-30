"""Вердикт по работе ученика: сверка с эталоном + пересчёт шагов (арх. §7).

Детерминированная логика, без LLM. Ошибочный шаг при верном ответе — «описка»
(slip), не ошибка. Непарсящийся ответ — uncertain, решает эскалация/уточнение,
а не наказание ребёнка ложной «ошибкой».
"""

import re
from typing import Literal

from pydantic import BaseModel

from hwcheck.pipeline.mathparse import parse_equation, parse_value
from hwcheck.pipeline.reading import review_reading
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.validator import (
    BINARY_OPERATOR,
    LineCheck,
    check_steps,
    compare_answers,
    last_value_matches,
)

Verdict = Literal["correct", "wrong", "uncertain"]
# почему «не уверен» — по ним решаем, какие уточняющие вопросы ученику окупятся
UncertainReason = Literal[
    "unreadable",  # на странице <неразборчиво>: вопрос «какой знак/цифра»
    "ambiguous_equation",  # корень сменился — новое уравнение или ошибка шага
    "answer_unparseable",  # ответ записан, но не разобран: «какой ответ получился?»
    "no_answer",  # ответа нет, последняя строка не совпала с эталоном
    "steps_unparseable",  # ни одна строка решения не разобрана
    "column_unreadable",  # деление уголком не прочитано: обрывки вместо записи
    "line_misread",  # расхождение строки — скорее чтение, чем ошибка (pipeline/reading.py)
    "no_solution",  # выражения без «=» и без ответа: «запишите разность» или одно условие
]
_UNREADABLE = "неразборчив"

# пункт задания: «а)», «б)» в условии или в начале строки решения
_ITEM = re.compile(r"(?:^|[\s;:,.])([а-еa-e])\)", re.IGNORECASE)
_STEP_ITEM = re.compile(r"^\s*([а-еa-e])\)", re.IGNORECASE)
# отдельное арифметическое выражение: числа (со скобками, смешанные «8 3/7»), соединённые
# знаками; «651 + 126 306 − 138» — два выражения, «15 · 10 + (30 − 20) · 5» — одно
_NUM = r"\(*\s*\d+(?:[.,]\d+)?(?:\s+\d+\s*/\s*\d+)?\s*\)*"
_EXPRESSION = re.compile(rf"{_NUM}(?:\s*[+\-−·×*:/]\s*{_NUM})+")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")
# деление уголком (живой альбом 14.09, №55): распознавание рвёт запись на обрывки («− 6», «14»)
# и склеивает мусорные равенства («748 * 374 = 279352»)
_FRAGMENT = re.compile(r"^\s*[+\-−×*·]?\s*\d+(?:[.,]\d+)?\s*$")
_DIVISION = re.compile(r"\d\s*[:|÷]\s*\d")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_CLOCK = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
# знак действия между числами → семейство: OCR пишет деление и «:», и «/», умножение — «*», «·»
_OPERATION = re.compile(r"(?<=\d)\s*([+\-−*·×:/÷])\s*(?=\d)")
_FAMILY = {"+": "+", "-": "-", "−": "-", "*": "*", "·": "*", "×": "*", ":": ":", "/": ":", "÷": ":"}
LONG_DIVISION_FRAGMENTS = 3


class GradeResult(BaseModel):
    verdict: Verdict
    answers_match: bool | None
    first_error_line: int | None  # 1-based, первый шаг с арифметическим расхождением
    slip_lines: list[int]  # шаги с расхождением при верном итоговом ответе
    line_checks: list[LineCheck]
    uncertain_reason: UncertainReason | None = None  # только для verdict=uncertain


def grade(
    student_steps: list[str],
    student_answer: str | None,
    ref: RefSolution,
    *,
    condition: str | None = None,
) -> GradeResult:
    """`condition` — печатное условие задания: помогает перечитать знаки, спутанные OCR."""
    reason: UncertainReason | None = None
    checks = check_student_steps(student_steps, condition=condition)
    listed = several_answers(ref.answer) and _SUBSTITUTION.search(condition or "") is not None
    if is_multipart(condition, student_steps) or listed:
        # эталон солвера — один ответ на несколько пунктов (живые логи 06.09: «80» на
        # четыре выражения) или список ответов на несколько подстановок («[339, 7254]»,
        # живая проверка 30.09); сверять итог не с чем, судим по арифметике каждой строки
        return grade_by_lines(checks, condition=condition)
    if is_long_division(student_steps, condition):
        return _grade_long_division(checks, condition, ref.answer, student_answer)
    mismatch_lines = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    answers_match = compare_answers(student_answer, ref.answer)
    if answers_match is None and not mismatch_lines and last_value_matches(checks, ref.answer):
        # ответа нет или он фразой с несколькими числами («Отв.: на 10 яблок надо 160»),
        # но вся арифметика верна и последняя строка даёт эталон (живые логи 07.09).
        # Несовпадение не значит ошибку — строка могла быть промежуточной → uncertain
        answers_match = True

    if answers_match is None and mismatch_lines:
        # ответа нет или он не читается, но арифметика доказуемо неверна (SymPy —
        # источник истины): «803 + 169 = 753» — ошибка, а не «не уверен»
        verdict: Verdict = "wrong"
        first_error = mismatch_lines[0]
        slips: list[int] = []
    elif answers_match is None:
        verdict = "uncertain"
        first_error = None
        slips = []
        reason = _uncertain_reason(checks, student_answer)
    elif answers_match:
        verdict = "correct"
        first_error = None
        slips = mismatch_lines
    else:
        verdict = "wrong"
        first_error = mismatch_lines[0] if mismatch_lines else None
        slips = []

    return GradeResult(
        verdict=verdict,
        answers_match=answers_match,
        first_error_line=first_error,
        slip_lines=slips,
        line_checks=checks,
        uncertain_reason=reason if verdict == "uncertain" else None,
    )


def check_student_steps(steps: list[str], *, condition: str | None = None) -> list[LineCheck]:
    """Пересчёт строк работы ученика: расхождение, похожее на ошибку чтения, — «не уверен».

    Только для тетради: эталон солвера проверяется `check_steps` без этого прохода.
    """
    return review_reading(check_steps(steps, condition=condition), condition)


def _uncertain_reason(checks: list[LineCheck], student_answer: str | None) -> UncertainReason:
    """Одна главная причина: сначала то, что лечится вопросом ученику, потом пробелы разбора."""
    answer = (student_answer or "").strip()
    if _UNREADABLE in answer.lower() or any(_UNREADABLE in c.line.lower() for c in checks):
        return "unreadable"
    if any(c.misread for c in checks):
        return "line_misread"
    if any(c.doubtful for c in checks):
        return "ambiguous_equation"
    if answer and parse_value(answer) is None:
        return "answer_unparseable"
    if not answer and _only_condition(checks):
        return "no_solution"
    if not any(c.status in ("ok", "mismatch") for c in checks):
        return "steps_unparseable"
    return "no_answer"


def _only_condition(checks: list[LineCheck]) -> bool:
    """Выражения без вычислений: ни «=», ни строки-ответа («а) 75» — это ответ). Так выглядит и
    переписанное условие, и решение задания «запишите разность» (живые проверки 30.09, №2.183 и
    №2.184) — различить их пересчёт не может."""
    lines = [c.line for c in checks if c.line.strip()]
    return bool(lines) and all("=" not in line and BINARY_OPERATOR.search(line) for line in lines)


def is_multipart(condition: str | None, steps: list[str]) -> bool:
    """Два и больше пунктов «а)», «б)» (в условии или строках решения) или несколько
    отдельных примеров в условии: «№52. 651 + 126; 379 − 253; …» (живой альбом 13.09)."""
    labels = {m.group(1).lower() for m in _ITEM.finditer(condition or "")}
    step_labels = {m.group(1).lower() for s in steps if (m := _STEP_ITEM.match(s))}
    return (
        len(labels) >= 2
        or len(step_labels) >= 2
        or _listed_expressions(condition) >= 2
        or _listed_equations(condition) >= 2
        or _substitutions(condition) >= 2
    )


# «… при c = 720, d = 382; c = 7112, d = 905» — наборы значений через «;»
_SUBSTITUTION = re.compile(r"(?<![А-Яа-яЁё])при\s+(.+)", re.IGNORECASE | re.DOTALL)
# список ответов солвера: «[2181, 20900]», «(316, 377)», «339; 7254». Под шаблон попадают и
# точка «(2; 3)», и дроби «0,5; 1,5» — поэтому он действует только при подстановке «при …» (ревью)
_ANSWER_LIST = re.compile(r"^\s*[\[(]?\s*-?\d+(?:\s*[,;]\s*-?\d+)+\s*[\])]?\s*$")


def _substitutions(condition: str | None) -> int:
    """Сколько наборов значений подставить: «n + 6775 при n = 657; 4315» — два."""
    match = _SUBSTITUTION.search(condition or "")
    if match is None:
        return 0
    return sum(1 for part in match.group(1).split(";") if re.search(r"\d", part))


def several_answers(answer: str | None) -> bool:
    """Эталон — несколько ответов (по одному на подстановку), а не одно число."""
    return bool(answer) and _ANSWER_LIST.search(answer or "") is not None


# разделители примеров в условии и текст-инструкция перед первым из них
_CHUNK_BREAK = re.compile(r"[;\n]|\s{3,}")
_LEADING_WORDS = re.compile(r"^(?:[А-Яа-яЁё]+[\s.,:!?—-]*)+")


def _listed_equations(condition: str | None) -> int:
    """Уравнения списком: «Реши уравнения. 180 − x = 100; x − 17 = 40» (стенд 14.09, hw2 №20).

    Эталон солвера — один ответ на все уравнения; присваивания «a = 5, b = 3» не считаются.
    """
    count = 0
    for chunk in _CHUNK_BREAK.split(condition or ""):
        equation = parse_equation(_LEADING_WORDS.sub("", chunk.strip()))
        count += int(equation is not None and equation.kind == "equation")
    return count


def _listed_expressions(condition: str | None) -> int:
    return len(condition_examples(condition))


def condition_examples(condition: str | None) -> list[str]:
    """Примеры списком после текста: «Вычисли. 3 · 196   2 · 438», «651 + 126; 379 − 253».

    Числа внутри текстовой задачи — «в 8:15, а прибывает в 10:45», «15-20 рублей» —
    не в счёт (ревью): иначе задача уходила бы в проверку по строкам без сверки с
    эталоном, и неверный ход решения с верной арифметикой получал «верно».
    """
    examples: list[str] = []
    for chunk in re.split(r"[;\n]", condition or ""):
        after_text = max((m.end() for m in _LETTER.finditer(chunk)), default=0)
        examples.extend(example.strip() for example in _EXPRESSION.findall(chunk[after_text:]))
    return examples


def is_long_division(steps: list[str], condition: str | None) -> bool:
    """Деление уголком: деление в работе или в примерах условия и обрывки записи столбиком.

    Простой столбик сложения («803 / +169 / 753») сюда не попадает — его ошибки валидатор
    по-прежнему ловит.
    """
    fragments = sum(1 for step in steps if _FRAGMENT.match(step))
    division = any(_DIVISION.search(step) for step in steps) or any(
        _is_division_example(example) for example in condition_examples(condition)
    )
    return division and fragments >= LONG_DIVISION_FRAGMENTS


def _is_division_example(example: str) -> bool:
    """«в 10:45» и «10 : 45» (OCR ставит пробелы) — время, не деление (ревью); пример с
    пробелами и целым результатом («20 : 10») — деление. Нецелое «18 : 24» уголком не
    подтвердить результатом, поэтому потерять его как признак деления не страшно."""
    if ":" not in example or _CLOCK.match(example):
        return False
    return not _CLOCK.match(re.sub(r"\s+", "", example)) or _integer_result(example) is not None


def _grade_long_division(
    checks: list[LineCheck],
    condition: str | None,
    ref_answer: str | None,
    student_answer: str | None,
) -> GradeResult:
    """Уголок распознавание не читает, поэтому расхождения обрывков — не ошибки ребёнка.

    Ошибка — строка примера из условия или его проверки («374 · 2» для «748 : 2») с неверным
    результатом, или неверный итоговый ответ; «верно» — верный ответ или результаты всех
    примеров условия нашлись в работе; иначе «не уверен».
    """
    examples = condition_examples(condition)
    errors = [
        i
        for i, check in enumerate(checks, start=1)
        if check.status == "mismatch" and _is_example_line(check.line, examples)
    ]
    answers_match = compare_answers(student_answer, ref_answer) if ref_answer else None
    results_found = bool(examples) and all(_result_found(e, examples, checks) for e in examples)
    if errors or answers_match is False:
        verdict: Verdict = "wrong"
    elif answers_match or results_found:
        verdict = "correct"
    else:
        verdict = "uncertain"
    return GradeResult(
        verdict=verdict,
        answers_match=answers_match,
        first_error_line=errors[0] if errors else None,
        slip_lines=[],
        line_checks=checks,
        uncertain_reason="column_unreadable" if verdict == "uncertain" else None,
    )


def _is_example_line(line: str, examples: list[str]) -> bool:
    """Пример условия теми же числами и действиями («748 : 2 = 375») или проверка деления
    умножением частного на делитель («374 * 2 = 700»).

    Мусорное «748 * 374 = 279352» из уголка — ни то ни другое; «9 + 9 = 100» при «81 : 9» —
    тоже (повторное ревью: частное равно делителю, но проверка — только умножение).
    """
    if "=" not in line:
        return False
    left = line.split("=", 1)[0]
    numbers = sorted(_NUMBER.findall(left))
    operations = _operations(left)
    for example in examples:
        operands = _NUMBER.findall(example)
        if numbers == sorted(operands) and operations == _operations(example):
            return True
        result = _integer_result(example)
        is_check = operations == ["*"] and _operations(example) == [":"] and result is not None
        if is_check and numbers == sorted([result, operands[1]]):
            return True
    return False


def _operations(expression: str) -> list[str]:
    return [_FAMILY[sign] for sign in _OPERATION.findall(expression)]


def _integer_result(example: str) -> str | None:
    """Целый результат примера; дробный в уголке не ищем — только «не уверен»."""
    value = parse_value(example)
    return str(value) if value is not None and getattr(value, "is_integer", False) else None


def _result_found(example: str, examples: list[str], checks: list[LineCheck]) -> bool:
    result = _integer_result(example)
    if result is None:
        return False
    if any(result in _NUMBER.findall(other) for other in examples):
        return False  # число есть в условии — не отличить результат ребёнка от условия
    return any(result in _NUMBER.findall(check.line) for check in checks)


def grade_by_lines(checks: list[LineCheck], *, condition: str | None = None) -> GradeResult:
    """Без эталонного ответа: только детерминированный пересчёт строк."""
    if is_long_division([check.line for check in checks], condition):
        return _grade_long_division(checks, condition, None, None)
    mismatches = [i for i, c in enumerate(checks, start=1) if c.status == "mismatch"]
    if mismatches:
        verdict: Verdict = "wrong"
    elif any(c.doubtful for c in checks):
        verdict = "uncertain"
    elif any(c.status == "ok" for c in checks):
        verdict = "correct"
    else:
        verdict = "uncertain"
    return GradeResult(
        verdict=verdict,
        answers_match=None,
        first_error_line=mismatches[0] if mismatches else None,
        slip_lines=[],
        line_checks=checks,
        uncertain_reason=_uncertain_reason(checks, None) if verdict == "uncertain" else None,
    )
