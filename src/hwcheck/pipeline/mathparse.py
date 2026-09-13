"""Безопасный парсер школьной математической нотации (арх. §3.3 Validator).

Вход — строка из тетради или от Solver'а: «8 3/7 - 4 4/7 = 3 6/7», «90:18=5»,
«4,7+0,3=5». Выход — точные значения сегментов между «=» (sympy Rational, без
float-погрешностей) или None, если строка не является проверяемым равенством.

Текст ученика недоверенный: перед sympy стоит жёсткий whitelist символов
и лимиты на размер чисел/степеней (sympy парсит через eval).
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import sympy
from sympy.parsing.sympy_parser import parse_expr, rationalize, standard_transformations

logger = logging.getLogger(__name__)

MAX_LINE_LENGTH = 200
MAX_NUMBER_DIGITS = 12
MAX_EXPONENT = 40

# маркер пункта в начале строки: «а)», «3)», «№4», «1. » (точка — только с пробелом,
# иначе съедим начало десятичной дроби «5.5»)
_ITEM_MARKER = re.compile(r"^\s*(№\s*\d+[.)]?|[а-яёa-z][).]|\d{1,2}\)|\d{1,2}\.\s)\s*")
_MIXED_NUMBER = re.compile(r"(?<![\d/.])(\d+)\s+(\d+)\s*/\s*(\d+)")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
# x — переменная уравнения («96 : x = 8»); в числовых строках её не бывает
_DIVISION_COLON = re.compile(r"(?<=[\dx)])\s*:\s*(?=[-\dx(])")
_FRACTION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?![\d.])")
_SQRT_BARE = re.compile(r"√\s*(\d+(?:[.,]\d+)?)")
_ALLOWED = re.compile(r"^[\d+\-*/(). ]*$")
# школьная запись результата: «Ответ: 300 человек», «= 300 (чел.) - отдохнуло в августе»
# «-» перед цифрой — знак числа («Ответ -5»), а не разделитель после метки
_ANSWER_LABEL = re.compile(r"^\s*отв(?:ет)?\s*(?:[:.—–]+|-(?!\d))?\s*", re.IGNORECASE)
_UNIT_PARENS = re.compile(r"\(\s*[^\d()]*\)")  # скобки без цифр: «(чел.)», «(км)»
# пояснение после тире начинается со слова и может содержать числа: «- на 16 яблок»;
# «300 - 2 яблока» (тире перед числом) — по-прежнему выражение, не пояснение
_EXPLANATION = re.compile(r"\s[-–—]\s*[а-яёА-ЯЁ].*$", re.S)
_LEADING_VALUE = re.compile(
    r"^\s*(?P<value>-?\d+(?:[.,]\d+)?(?:\s+\d+\s*/\s*\d+|\s*/\s*\d+)?)(?P<tail>.*)$", re.S
)
_LONG_NUMBER = re.compile(rf"\d{{{MAX_NUMBER_DIGITS + 1},}}")
# показатель степени — только «голое» число: составной показатель 2**(1+999999999)
# невидим для проверки величины и школе не нужен
_COMPOSITE_EXPONENT = re.compile(r"\*\*\s*[-(]")
_EXPONENT = re.compile(r"\*\*\s*(\d+)")

_TRANSFORMATIONS = (*standard_transformations, rationalize)

X = sympy.Symbol("x")
# переменная уравнения — одиночная буква, не часть слова: «x», «y», кириллическая «х»
_VARIABLE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])([A-Za-z]|х)(?![A-Za-zА-Яа-яЁё])")
_IMPLICIT_MUL_BEFORE = re.compile(r"(?<=[\d)])\s*(?=x)")  # «3x», «(2+1)x» → «3*x»
_IMPLICIT_MUL_AFTER = re.compile(r"(?<=x)\s*(?=[\d(])")  # «x(» , «x2» → «x*(»
_NUMBER_LITERAL = re.compile(r"^-?\d+(?:\.\d+)?(?:/\d+)?$")
_ANSWER_VARIABLE = re.compile(r"^\s*(?:[A-Za-z]|х)\s*=\s*(?=[-\d])")  # «x = 7» → «7»
MAX_EQUATION_DEGREE = 2


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


@dataclass
class EquationLine:
    """Строка уравнения с одной переменной (приведена к символу X)."""

    variable: str
    segments: list[Any]  # sympy-выражения сегментов между «=», свободный символ — только X
    solved_form: bool  # «x = число»: цепочка преобразований на этой строке закончилась


def parse_equation(line: str) -> EquationLine | None:
    """None — не уравнение с ровно одной переменной (текст, формула с двумя буквами)."""
    if len(line) > MAX_LINE_LENGTH or "=" not in line or "sqrt" in line:
        return None
    text = _normalize(_ITEM_MARKER.sub("", line))
    letters = {m.group(1) for m in _VARIABLE.finditer(text)}
    if len(letters) != 1:
        return None
    variable = letters.pop()
    text = _VARIABLE.sub("x", text)
    text = _IMPLICIT_MUL_AFTER.sub("*", _IMPLICIT_MUL_BEFORE.sub("*", text))
    raw_segments = [s.strip() for s in text.split("=")]
    if len(raw_segments) < 2 or not all(raw_segments):
        return None
    segments = []
    for segment in raw_segments:
        value = _eval_segment(segment, allow_variable=True)
        if value is None:
            return None
        segments.append(value)
    if not any(s.free_symbols for s in segments):
        return None
    solved_form = raw_segments[0] == "x" and bool(_NUMBER_LITERAL.match(raw_segments[-1]))
    return EquationLine(variable=variable, segments=segments, solved_form=solved_form)


def solve_single_root(left: Any, right: Any) -> Any | None:
    """Единственный корень уравнения left = right; None — корней нет, два и больше, тождество.

    Степень числителя ограничена: sympy.solve на произвольном вводе ребёнка может думать долго.
    """
    difference = sympy.together(left - right)
    try:
        degree = sympy.Poly(sympy.numer(difference), X).degree()
    except sympy.PolynomialError:
        return None
    if degree < 1 or degree > MAX_EQUATION_DEGREE:
        return None
    roots = [r for r in sympy.solve(sympy.Eq(left, right), X) if r.is_real]
    return roots[0] if len(roots) == 1 else None


def parse_value(text: str) -> Any | None:
    """Одиночное значение (ответ): «3 6/7», «4,5», «90 км/ч», «Ответ: 300 человек» → число.

    None — не парсится (→ uncertain).
    """
    if len(text) > MAX_LINE_LENGTH:
        return None
    text = _ANSWER_VARIABLE.sub("", _ANSWER_LABEL.sub("", text))
    leading = _leading_value(text)
    stripped = leading if leading is not None else _strip_units(text)
    if not stripped or len(stripped) > MAX_LINE_LENGTH or "=" in stripped:
        return None
    return _eval_segment(_normalize(stripped))


def _normalize(text: str) -> str:
    text = text.replace("−", "-").replace("·", "*").replace("×", "*").replace("∙", "*")
    text = _DECIMAL_COMMA.sub(".", text)
    text = _MIXED_NUMBER.sub(r"(\1+\2/\3)", text)
    text = _SQRT_BARE.sub(r"sqrt(\1)", text)
    text = text.replace("^", "**")
    return text.strip()


def _school_division(segment: str) -> str:
    """«:» делит с приоритетом умножения, но дробная черта связывает сильнее.

    «4/5 : 9/10» — это (4/5):(9/10); простая замена «:» на «/» давала 4/5/9/10 = 2/225
    и ложную «ошибку» на верном делении дробей (живые логи 13.09). Поэтому дробь
    становится атомом в скобках, и только потом «:» → «/». Рядом со «/» и «**» дробь
    не оборачивается: «12/6/2», «2**3/4» и «4/2**3» = 4/(2**3) — обычный приоритет.
    """

    def wrap(match: re.Match[str]) -> str:
        before = segment[: match.start()].rstrip()
        if before.endswith(("/", "**")) or segment[match.end() :].lstrip().startswith("**"):
            return match.group(0)
        return f"({match.group(1)}/{match.group(2)})"

    return _DIVISION_COLON.sub("/", _FRACTION.sub(wrap, segment))


def _eval_segment(segment: str, *, allow_variable: bool = False) -> Any | None:
    segment = _school_division(segment)
    without_functions = segment.replace("sqrt", "").replace("**", "*")
    if allow_variable:
        without_functions = without_functions.replace("x", "")
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
            local_dict={"sqrt": sympy.sqrt, "x": X},
            transformations=_TRANSFORMATIONS,
            evaluate=True,
        )
    except Exception as exc:
        # parse_expr исполняет преобразованный код через eval, и мусор из тетради
        # роняет его чем угодно: «40 . 40» → AttributeError (живые логи 06.09 —
        # падала обработка всего фото). Контракт один: не парсится → None
        logger.debug("parse_expr failed (%s) on %r", type(exc).__name__, segment)
        return None
    if not isinstance(value, sympy.Expr):
        return None
    if value.free_symbols - ({X} if allow_variable else set()):
        return None
    return value


def _leading_value(text: str) -> str | None:
    """Число в начале записи, если дальше только слова/единицы: «300 (чел.) - отдохну-» → «300».

    None, когда хвост содержит другие числа («2 км 300 м», «220 + 180»): такую запись
    честнее не понять (uncertain), чем обрезать и выдать ложную ошибку.
    """
    match = _LEADING_VALUE.match(_EXPLANATION.sub("", _UNIT_PARENS.sub(" ", text)))
    if match is None or re.search(r"\d", match.group("tail")):
        return None
    return match.group("value")


def _strip_units(text: str) -> str:
    # убираем кириллические слова-единицы («км/ч», «руб.») и лишнюю пунктуацию
    without_units = re.sub(r"[а-яёА-ЯЁ]+(?:/[а-яёА-ЯЁ]+)?\.?", " ", text)
    return without_units.strip(" .;")
