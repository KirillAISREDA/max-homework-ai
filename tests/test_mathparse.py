import pytest

from hwcheck.pipeline.mathparse import ParsedLine, parse_line

# (строка из тетради, ожидаемые значения сегментов между «=»)
PARSEABLE_CASES = [
    ("2+2=4", ["4", "4"]),
    ("6*7=42", ["42", "42"]),
    ("6·7=42", ["42", "42"]),
    ("6×7=42", ["42", "42"]),
    ("90:18=5", ["5", "5"]),
    ("4,7+0,3=5", ["5", "5"]),
    ("2^3=8", ["8", "8"]),
    ("sqrt(16)=4", ["4", "4"]),
    ("√16=4", ["4", "4"]),
    ("-(-21)=21", ["21", "21"]),
    # смешанные числа
    ("8 3/7 - 4 4/7 = 3 6/7", ["27/7", "27/7"]),
    ("3 1/2 + 1/2 = 4", ["4", "4"]),
    # цепочка равенств
    ("5+5=10=2*5", ["10", "10", "10"]),
    # маркер пункта в начале строки
    ("а) -(-21)=21", ["21", "21"]),
    ("№4 90:18=5", ["5", "5"]),
    ("3) 90:18=5", ["5", "5"]),
    # юникодный минус
    ("−5+6=1", ["1", "1"]),
]


@pytest.mark.parametrize(("line", "expected"), PARSEABLE_CASES)
def test_parse_line_values(line: str, expected: list[str]) -> None:
    parsed = parse_line(line)
    assert isinstance(parsed, ParsedLine)
    assert [str(v) for v in parsed.values] == expected


NOT_EQUATION = [
    "Ответ: 5 км/ч",  # текст, а не уравнение («:» здесь не деление)
    "решение задачи",
    "x + y = 3",  # переменные пока не поддерживаем — не проверяем, а не падаем
    "8 3/7 - <неразборчиво> = 3",
    "",
]


@pytest.mark.parametrize("line", NOT_EQUATION)
def test_unparseable_lines_return_none(line: str) -> None:
    assert parse_line(line) is None


def test_no_equals_sign_is_not_checkable() -> None:
    assert parse_line("2+2") is None


DANGEROUS = [
    "9**9**9=0",  # гигантская степень — защита от DoS
    "2^999=0",
    "12345678901234567890+1=0",  # слишком длинное число
    "__import__('os')=1",
]


@pytest.mark.parametrize("line", DANGEROUS)
def test_dangerous_input_rejected(line: str) -> None:
    assert parse_line(line) is None
