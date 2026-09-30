"""Сомнение в чтении строки (спецификация 2026-09-30-math-misread-guards-design.md).

Пересчёт строки доказывает ошибку ребёнка, только если строка прочитана верно. Два признака, что
это не так: строка расходится с печатным условием своего пункта — и левой частью, и результатом;
или её «верное значение» для школьного примера невозможно («3175254/61» — живая проверка 30.09,
распознавание прочитало «1 662 372» как «16623 * 42»). Такая строка — «не уверен», а не ошибка.

Проход идёт после `check_steps` и только по работе ученика: эталоны солвера и генератора
проверяются валидатором без послаблений.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import sympy

from hwcheck.pipeline.mathparse import VARIABLE, parse_value
from hwcheck.pipeline.validator import BINARY_OPERATOR, LineCheck


@dataclass(frozen=True)
class PrintedItem:
    """Пункт печатного условия: «а) 39 452 − 16 452 : (300 − 264)»."""

    expression: str  # без пробелов в разрядах: «39452 - 16452 : (300 - 264)»
    value: Any  # точное значение (SymPy)
    operators: int  # число действий — по нему строка-пример отличается от строки-действия


# метка пункта — буква или число до двух цифр со скобкой, в начале или после пробела и знаков
_LABEL = re.compile(r"(?:^|(?<=[\s;:.,]))([А-Яа-яЁёA-Za-z]|\d{1,2})\)")
_LINE_LABEL = re.compile(r"^\s*([А-Яа-яЁёA-Za-z]|\d{1,2})\)")
# пробел в разрядах «39 452»; «8 123/456» — смешанное число, не разряды
_THOUSANDS_GAP = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d)(?!\s*/))")
# пункт для сверки — только числовое выражение: без слов и без «=»
_NUMERIC_EXPRESSION = re.compile(r"^[\d\s+\-−*·×∙:/().,]+$")
_FRACTION_NOTATION = re.compile(r"/|\d[.,]\d")
# отрицательное число в записи: минус в начале, после «(», «=» или знака действия
_NEGATIVE_NUMBER = re.compile(r"(?:^|[(=+\-−*·×:/])\s*[-−]\s*[\d(]")
# распознавание пишет латинскую «a)» на месте рукописной «а)»
_LOOKALIKES = str.maketrans("ae", "ае")


def printed_items(condition: str | None) -> dict[str, PrintedItem]:
    """Пункты условия, пригодные для сверки; метка, встретившаяся дважды, не используется."""
    text = condition or ""
    marks = _top_level_labels(text)
    counts = Counter(_normal_label(m.group(1)) for m in marks)
    items: dict[str, PrintedItem] = {}
    for index, mark in enumerate(marks):
        label = _normal_label(mark.group(1))
        end = marks[index + 1].start(1) if index + 1 < len(marks) else len(text)
        item = _printed_item(text[mark.end() : end])
        if item is not None and counts[label] == 1:
            items[label] = item
    return items


def review_reading(checks: list[LineCheck], condition: str | None) -> list[LineCheck]:
    """Строки, чьё расхождение — скорее чтение, чем ошибка, становятся `skipped` с `misread`
    (и `doubtful`: для вердикта это то же «не уверен», `misread` только называет причину)."""
    items = printed_items(condition)
    return [_review(check, items) for check in checks]


def _review(check: LineCheck, items: dict[str, PrintedItem]) -> LineCheck:
    if check.status not in ("ok", "mismatch") or len(check.values) < 2:
        return check
    label = _LINE_LABEL.match(check.line)
    body = check.line[label.end() :] if label else check.line
    written = parse_value(check.values[-1])
    if written is None or VARIABLE.search(body):
        return check  # уравнения и присваивания («S = 6 * 4») здесь не трогаем
    item = items.get(_normal_label(label.group(1))) if label else None
    if item is not None and _operators(body.split("=", 1)[0]) >= item.operators:
        # ребёнок переписал пример целиком: выражение берём из условия, из тетради — остальное.
        # Всё после первого «=» — его вычисление: промежуточные значения цепочки тоже должны
        # сойтись («а) 2 * 3 + 4 = 5 + 4 = 10» — ошибка в середине, не «верно», ревью)
        computed = [parse_value(value) for value in check.values[1:]]
        if all(value is not None and _same(value, item.value) for value in computed):
            # у верной строки values — значение печатного выражения и записанный результат
            return check.model_copy(
                update={"status": "ok", "values": [str(item.value), check.values[-1]]}
            )
        left = parse_value(check.values[0])
        if left is None or not _same(left, item.value):
            return _doubt(check, printed=item.expression)
    if check.status == "mismatch" and _impossible(parse_value(check.values[0]), written, body):
        return _doubt(check)
    return check


def line_label(line: str) -> str | None:
    """Метка пункта в начале строки тетради («a)» → «а»); None — строка без метки."""
    label = _LINE_LABEL.match(line)
    return _normal_label(label.group(1)) if label else None


def _impossible(expected: Any, written: Any, line: str) -> bool:
    """«Верное значение», какого у школьного примера на целые числа не бывает.

    Дробь без конечной десятичной записи, когда в строке нет ни дробей, ни десятичных чисел
    («10 : 3 = 3» — деление с остатком, а не ошибка); отрицательное число, когда ребёнок
    отрицательных не пишет и ответ не отличается от верного лишь знаком («12 − 20 = 8» — ошибка).
    """
    if expected is None or not (expected.is_Rational and written.is_Rational):
        return False
    if not expected.is_Integer:
        return not _FRACTION_NOTATION.search(line) and _without_2_and_5(int(expected.q)) != 1
    return bool(
        expected < 0 <= written and written != -expected and not _NEGATIVE_NUMBER.search(line)
    )


def _without_2_and_5(denominator: int) -> int:
    for factor in (2, 5):
        while denominator % factor == 0:
            denominator //= factor
    return denominator


def _printed_item(text: str) -> PrintedItem | None:
    expression = " ".join(_THOUSANDS_GAP.sub("", text).split()).rstrip(";., ")
    if not expression or not _NUMERIC_EXPRESSION.match(expression):
        return None
    operators = _operators(expression)
    value = parse_value(expression) if operators else None
    if value is None:
        return None
    return PrintedItem(expression=expression, value=value, operators=operators)


def _operators(expression: str) -> int:
    return len(BINARY_OPERATOR.findall(expression))


def _top_level_labels(text: str) -> list[re.Match[str]]:
    """Метки вне скобок выражения: «(5244 : 19 : 12)» не даёт метку «12)». Один проход по тексту;
    «)» самих меток («а)») не уводит глубину ниже нуля."""
    marks: list[re.Match[str]] = []
    depth = 0
    position = 0
    for mark in _LABEL.finditer(text):
        for char in text[position : mark.start(1)]:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
        position = mark.start(1)
        if depth == 0:
            marks.append(mark)
    return marks


def _normal_label(label: str) -> str:
    return label.lower().translate(_LOOKALIKES)


def _same(a: Any, b: Any) -> bool:
    """Рациональные сравниваются точно и дёшево, корни и прочее — через simplify (как валидатор)."""
    if a.is_Rational and b.is_Rational:
        return bool(a == b)
    return bool(sympy.simplify(a - b) == 0)


def _doubt(check: LineCheck, *, printed: str | None = None) -> LineCheck:
    return check.model_copy(
        update={"status": "skipped", "doubtful": True, "misread": True, "printed": printed}
    )
