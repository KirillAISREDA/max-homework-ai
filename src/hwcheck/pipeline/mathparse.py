"""Безопасный парсер школьной математической нотации (арх. §3.3 Validator).

Вход — строка из тетради или от Solver'а: «8 3/7 - 4 4/7 = 3 6/7», «90:18=5»,
«4,7+0,3=5». Выход — точные значения сегментов между «=» (sympy Rational, без
float-погрешностей) или None, если строка не является проверяемым равенством.

Текст ученика недоверенный: перед sympy стоит жёсткий whitelist символов
и лимиты на размер чисел/степеней (sympy парсит через eval).
"""

import re
from dataclasses import dataclass
from typing import Any

import sympy
from sympy.parsing.sympy_parser import parse_expr, rationalize, standard_transformations

MAX_LINE_LENGTH = 200
MAX_NUMBER_DIGITS = 12
MAX_EXPONENT = 40

# маркер пункта в начале строки: «а)», «3)», «№4», «1. » (точка — только с пробелом,
# иначе съедим начало десятичной дроби «5.5»)
_ITEM_MARKER = re.compile(r"^\s*(№\s*\d+[.)]?|[а-яёa-z][).]|\d{1,2}\)|\d{1,2}\.\s)\s*")
_MIXED_NUMBER = re.compile(r"(?<![\d/.])(\d+)\s+(\d+)\s*/\s*(\d+)")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_DIVISION_COLON = re.compile(r"(?<=[\d)])\s*:\s*(?=[-\d(])")
_SQRT_BARE = re.compile(r"√\s*(\d+(?:[.,]\d+)?)")
_ALLOWED = re.compile(r"^[\d+\-*/(). ]*$")
# школьная запись результата: «Ответ: 300 человек», «= 300 (чел.) - отдохнуло в августе»
# «-» перед цифрой — знак числа («Ответ -5»), а не разделитель после метки
_ANSWER_LABEL = re.compile(r"^\s*ответ\s*(?:[:.—–]|-(?!\d))?\s*", re.IGNORECASE)
_UNIT_PARENS = re.compile(r"\(\s*[^\d()]*\)")  # скобки без цифр: «(чел.)», «(км)»
_LEADING_VALUE = re.compile(
    r"^\s*(?P<value>-?\d+(?:[.,]\d+)?(?:\s+\d+\s*/\s*\d+|\s*/\s*\d+)?)(?P<tail>.*)$", re.S
)
_LONG_NUMBER = re.compile(rf"\d{{{MAX_NUMBER_DIGITS + 1},}}")
# показатель степени — только «голое» число: составной показатель 2**(1+999999999)
# невидим для проверки величины и школе не нужен
_COMPOSITE_EXPONENT = re.compile(r"\*\*\s*[-(]")
_EXPONENT = re.compile(r"\*\*\s*(\d+)")

_TRANSFORMATIONS = (*standard_transformations, rationalize)


@dataclass
class ParsedLine:
    """Проверяемое равенство: значения всех сегментов между «=»."""

    values: list[Any]  # sympy-выражения без свободных символов

    @property
    def consistent(self) -> bool:
        first = self.values[0]
        return all(sympy.simplify(v - first) == 0 for v in self.values[1:])


def parse_line(line: str) -> ParsedLine | None:
    """None — строка не является проверяемым равенством (текст, переменные, мусор)."""
    if len(line) > MAX_LINE_LENGTH or "=" not in line:
        return None
    normalized = _normalize(_ITEM_MARKER.sub("", line))
    segments = [s.strip() for s in normalized.split("=")]
    if len(segments) < 2 or not all(segments):
        return None
    # у результата отбрасываем школьный хвост: «300 (чел.) - отдохнуло в августе» → «300»
    result = _leading_value(segments[-1])
    if result is not None:
        segments[-1] = result

    values = []
    for segment in segments:
        value = _eval_segment(segment)
        if value is None:
            return None
        values.append(value)
    return ParsedLine(values=values)


def parse_value(text: str) -> Any | None:
    """Одиночное значение (ответ): «3 6/7», «4,5», «90 км/ч», «Ответ: 300 человек» → число.

    None — не парсится (→ uncertain).
    """
    if len(text) > MAX_LINE_LENGTH:
        return None
    text = _ANSWER_LABEL.sub("", text)
    leading = _leading_value(text)
    stripped = leading if leading is not None else _strip_units(text)
    if not stripped or len(stripped) > MAX_LINE_LENGTH or "=" in stripped:
        return None
    return _eval_segment(_normalize(stripped))


def _normalize(text: str) -> str:
    text = text.replace("−", "-").replace("·", "*").replace("×", "*").replace("∙", "*")
    text = _DECIMAL_COMMA.sub(".", text)
    text = _DIVISION_COLON.sub("/", text)
    text = _MIXED_NUMBER.sub(r"(\1+\2/\3)", text)
    text = _SQRT_BARE.sub(r"sqrt(\1)", text)
    text = text.replace("^", "**")
    return text.strip()


def _eval_segment(segment: str) -> Any | None:
    without_functions = segment.replace("sqrt", "").replace("**", "*")
    if not _ALLOWED.match(without_functions):
        return None
    if _LONG_NUMBER.search(segment):
        return None
    if segment.count("**") > 1:  # вложенные степени (9**9**9) — DoS
        return None
    if _COMPOSITE_EXPONENT.search(segment):
        return None
    for match in _EXPONENT.finditer(segment):
        if int(match.group(1)) > MAX_EXPONENT:
            return None
    try:
        # rationalize: 4.7 → 47/10, арифметика точная, без float-погрешностей
        value = parse_expr(
            segment,
            local_dict={"sqrt": sympy.sqrt},
            transformations=_TRANSFORMATIONS,
            evaluate=True,
        )
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, sympy.SympifyError):
        return None
    if not isinstance(value, sympy.Expr) or value.free_symbols:
        return None
    return value


def _leading_value(text: str) -> str | None:
    """Число в начале записи, если дальше только слова/единицы: «300 (чел.) - отдохну-» → «300».

    None, когда хвост содержит другие числа («2 км 300 м», «220 + 180»): такую запись
    честнее не понять (uncertain), чем обрезать и выдать ложную ошибку.
    """
    match = _LEADING_VALUE.match(_UNIT_PARENS.sub(" ", text))
    if match is None or re.search(r"\d", match.group("tail")):
        return None
    return match.group("value")


def _strip_units(text: str) -> str:
    # убираем кириллические слова-единицы («км/ч», «руб.») и лишнюю пунктуацию
    without_units = re.sub(r"[а-яёА-ЯЁ]+(?:/[а-яёА-ЯЁ]+)?\.?", " ", text)
    return without_units.strip(" .;")
