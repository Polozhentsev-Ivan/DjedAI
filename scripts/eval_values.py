"""Compare tool collections without discarding categories, ordering or multiplicity."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import chain, islice, product
from typing import Any

MSK = timezone(timedelta(hours=3))


def _calendar(value: Any, kind: str | None) -> Any:
    if kind is None or not isinstance(value, (str, date)):
        return value
    text = str(value)
    if kind == "day":
        return _day_label(value)
    if kind == "month" and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", text):
        return text
    if kind == "quarter" and (match := re.fullmatch(r"(\d{4})[ -]?Q([1-4])", text, re.I)):
        return f"{match[1]}-Q{match[2]}"
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        return value
    if instant.tzinfo:
        instant = instant.astimezone(MSK)
    if instant.day != 1 or any((instant.hour, instant.minute, instant.second, instant.microsecond)):
        return value
    if kind == "month":
        return instant.strftime("%Y-%m")
    if instant.month not in {1, 4, 7, 10}:
        return value
    return f"{instant.year}-Q{(instant.month - 1) // 3 + 1}"


def _day_label(value: Any) -> Any:
    """Tools may report a calendar day as a midnight timestamp."""
    try:
        instant = datetime.fromisoformat(str(value))
    except ValueError:
        return value
    if instant.tzinfo:
        instant = instant.astimezone(MSK)
    if any((instant.hour, instant.minute, instant.second, instant.microsecond)):
        return value
    return instant.strftime("%Y-%m-%d")


def _key(value: Any) -> Any:
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            return ("invalid_number", str(value))
        # Binary float noise (1.1 * 737) is not a difference in the data.
        return ("number", round(number, 6))
    if isinstance(value, (list, tuple)):
        return ("row", tuple(_key(cell) for cell in value))
    if isinstance(value, (str, date)):
        return ("text", str(value))
    return (type(value).__name__, str(value))


def collection_matches(
    fact: dict, groups: Iterable[list[Any]], *, column_aliases: dict[str, str] | None = None
) -> bool:
    """Allow column projections; complete rows and their pairings must still match.

    Only explicitly named top-N facts may select top rows from a larger collection.
    Projection search is capped; it never materializes combinations of data values.
    """
    expected = fact["value"]
    unordered = fact["comparison"] == "exact_unordered"
    aliases = (column_aliases or {}) if fact["key"] == "dataset.columns" else {}

    def scalar_key(value: Any) -> Any:
        return _key(aliases.get(value, value) if isinstance(value, str) else value)

    if all(not isinstance(row, (list, tuple, dict)) for row in expected):
        target = [scalar_key(cell) for cell in expected]
        for group in groups:
            # A single SQL column is also a flat collection. Do not flatten
            # arbitrary table rows: unrelated cells must not become evidence.
            if group and all(isinstance(row, (list, tuple)) and len(row) == 1 for row in group):
                group = [row[0] for row in group]
            if any(isinstance(cell, (list, tuple, dict)) for cell in group):
                continue
            actual = [scalar_key(cell) for cell in group]
            matches = Counter(actual) == Counter(target) if unordered else actual == target
            if matches:
                return True
        return False
    if not expected or any(not isinstance(row, (list, tuple)) for row in expected):
        return False
    width = len(expected[0])
    if not width or any(len(row) != width for row in expected):
        return False
    labels = [str(row[0]) for row in expected]
    calendar = None
    if all(re.fullmatch(r"\d{4}-\d{2}", value) for value in labels):
        calendar = "month"
    elif all(re.fullmatch(r"\d{4}-Q[1-4]", value) for value in labels):
        calendar = "quarter"
    elif all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in labels):
        calendar = "day"
    # Row order is a requirement only for time series and rankings. Labelled categories
    # such as weekdays are paired by label, whatever the ORDER BY of the query was.
    ranked = "порядок" in str(fact.get("unit")) or fact["key"].rsplit(".", 1)[-1].startswith("top")
    if calendar is None and not ranked:
        unordered = True

    def cell_key(value: Any, index: int) -> Any:
        return _key(_calendar(value, calendar) if index == 0 else value)

    target = [tuple(cell_key(cell, i) for i, cell in enumerate(row)) for row in expected]
    target_counts = Counter(target)
    top = re.search(r"(?:^|\.)top([1-9]\d*)$", fact["key"])
    top_n = int(top[1]) if top and width == 2 and int(top[1]) == len(expected) else None
    for group in groups:
        if not group or any(not isinstance(row, (list, tuple)) for row in group):
            continue
        actual_width = len(group[0])
        if actual_width < width or any(len(row) != actual_width for row in group):
            continue
        if len(group) != len(expected) and (top_n is None or len(group) < top_n):
            continue
        candidates = []
        for position in range(width):
            wanted = Counter(row[position] for row in target)
            options = []
            for index in range(actual_width):
                values = Counter(cell_key(row[index], position) for row in group)
                matches = wanted <= values if top_n else wanted == values
                if matches:
                    options.append(index)
            candidates.append(options)
        if any(not indices for indices in candidates):
            continue
        mappings = chain([tuple(range(width))], islice(product(*candidates), 1024))
        for mapping in mappings:
            if len(set(mapping)) != width:
                continue
            projected = [[row[index] for index in mapping] for row in group]
            if top_n:
                projected = [row for row in projected if row[0] is not None]
                if any(
                    isinstance(row[1], bool)
                    or not isinstance(row[1], (int, float, Decimal))
                    or not Decimal(str(row[1])).is_finite()
                    for row in projected
                ):
                    continue
                projected.sort(key=lambda row: (-Decimal(str(row[1])), str(row[0])))
                projected = projected[:top_n]
            actual = [tuple(cell_key(cell, i) for i, cell in enumerate(row)) for row in projected]
            matches = Counter(actual) == target_counts if unordered else actual == target
            if matches:
                return True
    return False
