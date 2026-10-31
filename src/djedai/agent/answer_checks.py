"""Проверки ответа кодом: не зависят от силы модели и не требуют её вызова."""

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from djedai.agent.verification import query_parameter_spans

_SECONDS_COLUMN_RE = re.compile(r"(?:^|_)(?:secs?|seconds?|sekund\w*)$", re.IGNORECASE)
_MIN_SECONDS_TO_CONVERT = 3600
_MAX_DURATIONS = 50
# Число с разделителями разрядов (пробел, неразрывные пробелы, запятая) и дробной частью.
_NUMBER_RE = re.compile(
    r"(?<![\w.,])-?\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?(?![\w])"
    r"|(?<![\w.,])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])"
    r"|(?<![\w.,])-?\d+(?:[.,]\d+)?(?![\w])"
)
# Целые до сотни — дни месяца, порядковые номера и множители формул, а не значения данных.
_SMALL_INTEGER = 100
_CODE_RE = re.compile(r"`[^`]*`")
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}\.\d{1,2}\.\d{2,4}\b|\b(?:19|20)\d{2}\b"
)


def _number(value: object) -> Decimal | None:
    """Число значения результата; DECIMAL приходит из инструментов строкой."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def duration_hints(columns: list[str], rows: Iterable[list[object]]) -> list[dict]:
    """Значения колонок в секундах, переведённые в часы для ответа пользователю."""
    indexes = [i for i, name in enumerate(columns) if _SECONDS_COLUMN_RE.search(str(name))]
    hints: list[dict] = []
    for row in rows:
        for index in indexes:
            value = _number(row[index]) if index < len(row) else None
            if value is None or abs(value) < _MIN_SECONDS_TO_CONVERT:
                continue
            hints.append(
                {
                    "column": columns[index],
                    "seconds": float(value),
                    "hours": round(float(value) / 3600, 2),
                }
            )
            if len(hints) >= _MAX_DURATIONS:
                return hints
    return hints


def _parse(text: str) -> Decimal | None:
    clean = re.sub(r"[   ]", "", text)
    if "," in clean and "." in clean:
        clean = clean.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", clean):
        clean = clean.replace(",", "")
    else:
        clean = clean.replace(",", ".")
    try:
        return Decimal(clean)
    except InvalidOperation:
        return None


def _is_total_row(row: list[object]) -> bool:
    """Строка ROLLUP: группы пусты, есть только числа. Прибавить её — посчитать итог вдвое."""
    groups = [value for value in row if _number(value) is None]
    return len(groups) > 0 and all(value is None for value in groups)


def column_totals(columns: list[str], rows: list[list[object]]) -> dict[str, int | float]:
    """Суммы числовых колонок: складывать столбец в уме модель может с ошибкой на порядок."""
    if len(rows) < 2 or any(_is_total_row(row) for row in rows):
        return {}
    totals: dict[str, int | float] = {}
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        numbers = [_number(value) for value in values]
        if numbers and all(number is not None for number in numbers):
            total = sum(numbers)
            totals[name] = int(total) if total == total.to_integral_value() else float(total)
    return totals


def _numbers_in(value: object) -> Iterable[Decimal]:
    """Все числа значения любой вложенности, включая написанные в тексте факта."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers_in(item)
    elif isinstance(value, str):
        for match in _NUMBER_RE.finditer(value):
            if (number := _parse(match[0])) is not None:
                yield number
    elif (number := _number(value)) is not None:
        yield number


def _known_values(entries: list[dict]) -> list[Decimal]:
    """Числа результатов, а также суммы колонок для итоговых строк."""
    known: list[Decimal] = []
    for entry in entries:
        known.extend(_numbers_in(entry.get("facts")))
        data = entry.get("data") or {}
        known.extend(_numbers_in(data.get("rows")))
        known.extend(_numbers_in(data.get("row_count")))
        totals: dict[int, Decimal] = {}
        for row in data.get("rows") or []:
            for index, value in enumerate(row):
                if (number := _number(value)) is not None:
                    totals[index] = totals.get(index, Decimal(0)) + number
        known.extend(totals.values())
    return known


def _matches(shown: Decimal, known: Decimal) -> bool:
    """Показанное число — это значение данных с округлением, в том числе как доля в процентах."""
    decimals = max(-shown.as_tuple().exponent, 0)
    step = Decimal(1).scaleb(-decimals)
    # Округление до показанной точности; половина шага — запас на другой режим округления.
    return any(abs(abs(shown) - abs(candidate)) <= step / 2 for candidate in (known, known * 100))


def unsupported_numbers(
    draft: str,
    entries: list[dict],
    query: str | Sequence[str] | None = None,
) -> list[str]:
    """Числа черновика, которых нет в данных результата: модель досчитала их сама.

    ``query`` — вопрос и SQL текущего хода. Явно названные там константы не
    считаются выдуманными: их окончательно проверяет ``verify_numeric_text``,
    которая требует ещё и подпись-константу в самом ответе.
    """
    known = _known_values(entries)
    if not known:
        return []
    text = _DATE_RE.sub(" ", _CODE_RE.sub(" ", draft))
    parameter_spans = query_parameter_spans(text, query)
    unsupported = []
    for match in _NUMBER_RE.finditer(text):
        shown = _parse(match[0])
        if shown is None or (shown == shown.to_integral_value() and abs(shown) <= _SMALL_INTEGER):
            continue
        if any(_matches(shown, value) for value in known):
            continue
        if match.start() in parameter_spans:
            continue
        unsupported.append(match[0])
    return list(dict.fromkeys(unsupported))
