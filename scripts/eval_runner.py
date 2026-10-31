"""Evaluation Runner for the BI Agent.

Runs the golden question set through the real agent graph, against the pinned
golden dataset loaded into DuckDB, and writes a report with per-case status,
duration, tool calls and numerical checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import numbers
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
# The project is not installed as a package: make src and the sibling scripts
# importable both for ``python scripts/eval_runner.py`` and ``scripts.eval_runner``.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_values import collection_matches  # noqa: E402
from verify_golden import check_identity, download_file  # noqa: E402

from djedai.agent.config import get_agent_client  # noqa: E402
from djedai.agent.dashboard import (  # noqa: E402
    chart_for_result,
    dashboard_preview_blocks,
    dashboard_refresh_blocks,
)
from djedai.agent.diagnostics import RunDiagnostics  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.analytics.registry import TOOLS_REGISTRY, execute_tool  # noqa: E402
from djedai.config import get_limits  # noqa: E402
from djedai.contracts.agent import Message  # noqa: E402
from djedai.contracts.catalog import DatasetCatalog  # noqa: E402
from djedai.contracts.chart import ChartSpec, ChartTemplate  # noqa: E402
from djedai.contracts.dashboard import (  # noqa: E402
    DashboardSnapshot,
    WidgetSnapshot,
)
from djedai.contracts.tool_result import ToolResult  # noqa: E402
from djedai.data.service import CsvSource, ingest_csv_dataset  # noqa: E402
from djedai.execution import ExecutionBudget  # noqa: E402

DEFAULT_QUESTIONS = ROOT / "eval/questions.yaml"
DEFAULT_MANIFEST = ROOT / "eval/datasets/hk_background_jobs.json"

# Define current agent capabilities.
# Used to mark scenarios as skipped if they require unsupported features.
SUPPORTED_TOOLS = {definition.name for definition in TOOLS_REGISTRY}
SUPPORTED_REQUIREMENTS = {"memory"}
if {"seasonality", "time_aggregate"} <= SUPPORTED_TOOLS:
    SUPPORTED_REQUIREMENTS.add("seasonality")
if {"segment_profile", "compare_segments"} <= SUPPORTED_TOOLS:
    SUPPORTED_REQUIREMENTS.add("segments")
if {"correlation", "lagged_correlation"} <= SUPPORTED_TOOLS:
    SUPPORTED_REQUIREMENTS.add("correlation")
if "forecast" in SUPPORTED_TOOLS:
    SUPPORTED_REQUIREMENTS.add("forecast")
SUPPORTED_REQUIREMENTS.add("charts")
SUPPORTED_REQUIREMENTS.add("dashboards")

# Terminal failures published by the graph when the model or the verifier API
# is unavailable. Ordinary rejection/clarification notices do not match them.
TERMINAL_ERROR_MARKERS = ("API Error:", "Ошибка проверки ответа")
INPUT_ERROR_REASON = "Input validation happens in the UI/API before the agent graph"

# Spaces are accepted as thousand separators, so "500 000" is one number.
NUMBER_RE = re.compile(
    r"(?<![\w.,-])-?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
    r"(?:\s*(?:тыс|млн|млрд)\b\.?)?"
    r"(?=$|[^0-9.,]|[.,](?!\d))",
    re.IGNORECASE,
)
DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
# Dates and years refer to calendar positions, not to measured numbers.
RU_MONTH = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
DATE_ONLY_RE = re.compile(
    DATETIME_RE.pattern + r"|\d{4}-\d{2}-\d{2}"
    r"|\d{4}-\d{2}"
    r"|\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    rf"|\b\d{{1,2}}\s+{RU_MONTH}\s+\d{{4}}\b"
    rf"|\b{RU_MONTH}\s+\d{{4}}\b"
    r"|\b(?:19|20)\d{2}\s*(?:г\.(?!\w)|год[ауе]?\b)"
    r"|\b\d{4}[ -]?Q[1-4]\b|\bQ[1-4]\s+\d{4}\b"
    r"|\b[1-4](?:-й)?\s+квартал(?:а|е|у)?\s+\d{4}\b",
    re.IGNORECASE,
)
SCOPE_CLAIM_RE = re.compile(
    r"(?:выборк|данн|информаци)\w*[^.\n]{0,60}\b(?:только|лишь)\s+(?:по|за|об?)\s+(?:одн\w+|\d+)[^.\n]*",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(r"\blimit\s+(\d+)\s*;?\s*$", re.IGNORECASE)
ROWS_NOUN_RE = re.compile(r"\s*строк", re.IGNORECASE)
WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")
WEEKDAY_LABEL_RE = re.compile(rf"\b((?:{'|'.join(WEEKDAY_STEMS)})\w*)\s*\((\d)\)", re.IGNORECASE)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently losing expectations."""


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Read a graph object as either a model or a plain mapping (tests use both)."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _walk(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _is_finite_number(value: Any) -> bool:
    """Check if value is a real finite number, including numpy types and Decimals."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal, numbers.Real)):
        try:
            return math.isfinite(float(value))
        except (ValueError, TypeError, OverflowError):
            return False
    return False


def _decimal(value: Any) -> Decimal | None:
    """Numeric value of a cell, excluding booleans and non-finite numbers."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, numbers.Real)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text_numbers(text: str) -> Iterator[Decimal]:
    # Datetimes share digits with ordinary numbers; exclude them before parsing.
    for match in NUMBER_RE.finditer(DATETIME_RE.sub(" ", text)):
        raw = match.group(0)

        scale = 1
        lower_raw = raw.lower()
        if "тыс" in lower_raw:
            scale = 1000
        elif "млн" in lower_raw:
            scale = 1_000_000
        elif "млрд" in lower_raw:
            scale = 1_000_000_000

        clean = re.sub(r"[^\d.,+-]", "", raw).replace(",", ".").rstrip(".")
        try:
            number = Decimal(clean) * scale
            if number.is_finite():
                yield number
        except InvalidOperation:
            continue


def _claim_text(text: str) -> str:
    """Exclude calendar labels, identifiers and list markers, preserving numeric literals."""
    # Normalize U+2212 before either numeric matcher can discard its sign.
    text = text.replace("\N{MINUS SIGN}", "-")
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", " ", text)
    # A numeric prefix in a SQL identifier is not a claim about the data. Bare
    # numbers, including backtick-formatted numbers, must remain checkable.
    text = re.sub(r"\b\d+_[^\W\d]\w*\b", " ", text)
    return DATE_ONLY_RE.sub(" ", text)


def _claim_numbers(text: str) -> list[Decimal]:
    return list(_text_numbers(_claim_text(text)))


def _measurements(text: str) -> Iterator[tuple[Decimal, str]]:
    text = _claim_text(text)
    for match in NUMBER_RE.finditer(text):
        values = list(_text_numbers(match[0]))
        if not values:
            continue
        suffix = text[match.end() : match.end() + 30]
        prefix = text[max(0, match.start() - 30) : match.start()]
        if re.match(r"\s*(?:%|процент|percent)", suffix, re.I):
            policy = "percent"
        elif re.search(
            r"(?:доля|отношение|коэффициент)\s*(?:[:=]|составляет)?\s*$", prefix, re.I
        ) or re.match(r"\s*(?:раз[а]?\b|times\b)", suffix, re.I):
            policy = "ratio"
        else:
            policy = "value"
        yield values[0], policy


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not DATETIME_RE.fullmatch(value.strip()):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _same_instant(left: datetime, right: datetime) -> bool:
    if (left.tzinfo is None) != (right.tzinfo is None):
        return left.replace(tzinfo=None) == right.replace(tzinfo=None)
    return left == right


def _ok_results(results: Sequence[Any]) -> Iterator[Any]:
    for result in results:
        if _field(result, "status") in {"ok", "empty"} and not _field(result, "error"):
            yield result


def _data_facts(result: Any) -> Iterator[Any]:
    """Fact values that describe data; execution summaries are not answers."""
    for fact in _field(result, "facts") or []:
        if _field(fact, "kind") != "summary":
            yield _field(fact, "values") or {}


def _structured_scalars(results: Sequence[Any]) -> Iterator[Any]:
    """Every scalar cell and fact value produced by the tools."""
    for result in _ok_results(results):
        data = _field(result, "data")
        if data is not None:
            for row in _field(data, "rows") or []:
                yield from _walk(row)
        for values in _data_facts(result):
            for value in values.values():
                # A tool that reports a list of 15 columns also reports the count 15.
                if isinstance(value, list):
                    yield len(value)
            yield from _walk(values)


def _numbers_in(value: Any) -> Iterator[Decimal]:
    for item in _walk(value):
        number = _decimal(item)
        if number is not None:
            yield number


def _structured_numbers(results: Sequence[Any]) -> Iterator[Decimal]:
    for value in _structured_scalars(results):
        # DECIMAL приходит из инструментов строкой: без этого денежные суммы не сверить.
        if isinstance(value, str):
            try:
                value = Decimal(value)
            except InvalidOperation:
                continue
        number = _decimal(value)
        if number is not None:
            yield number


def _available_numbers(results: Sequence[Any]) -> list[Decimal]:
    """Every number a tool reported, including execution summaries such as row counts."""
    numbers = list(_structured_numbers(results))
    for result in _ok_results(results):
        for fact in _field(result, "facts") or []:
            if _field(fact, "kind") == "summary":
                numbers.extend(_numbers_in(_field(fact, "values") or {}))
    return numbers


def _structured_groups(results: Sequence[Any]) -> Iterator[list[Any]]:
    """Candidate row collections: result rows and list-valued facts."""
    for result in _ok_results(results):
        data = _field(result, "data")
        if data is not None and (_field(data, "rows") or []):
            yield list(_field(data, "rows"))
        for values in _data_facts(result):
            for value in values.values():
                if isinstance(value, list) and value:
                    yield value


def _scalar_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected is actual
    expected_number = _decimal(expected)
    if expected_number is not None:
        actual_number = _decimal(actual)
        return actual_number is not None and actual_number == expected_number
    if isinstance(expected, str):
        expected_datetime = _parse_datetime(expected)
        if expected_datetime is not None:
            actual_datetime = _parse_datetime(actual)
            return actual_datetime is not None and _same_instant(expected_datetime, actual_datetime)
        return isinstance(actual, str) and actual == expected
    if isinstance(actual, (list, tuple, dict)):
        return False
    return expected == actual


def _tolerance(fact: dict) -> Decimal:
    if fact["comparison"] != "approx":
        return Decimal(0)
    return Decimal(str(fact["abs_tolerance"]))


def _reused_results(
    expect: dict,
    context_results: Sequence[dict] | None,
) -> tuple[list[Any], str | None, list[str]]:
    """Tool results of the prerequisite that ``reuse_result_from`` names.

    A follow-up may legitimately answer from the previous turn without calling a tool
    again.  Only the explicitly declared prerequisite is evidence; other context
    results stay out so one case cannot borrow numbers from an unrelated question.
    """
    source = expect.get("reuse_result_from")
    if not source:
        return [], None, []
    for context in context_results or []:
        if _field(context, "id") != source:
            continue
        reused = list(_field(context, "tool_results") or [])
        if not reused:
            return [], source, [f"Context result '{source}' has no tool results to verify reuse"]
        return reused, source, []
    return [], source, [f"Context result '{source}' was not found to verify reuse"]


CALCULATION_CONSTANTS = {Decimal(day) for day in (28, 29, 30, 31, 100)}


def _answer_issues(case: dict, text: str, results: Sequence[Any]) -> list[str]:
    """Numbers in the answer must originate in tool results; user constants are exempt."""
    expect = case.get("expect") or {}
    if not (expect.get("answer_contains_value") or {}).get("from_result"):
        return []

    numbers = _available_numbers(results)
    # Unique values, preserving order (summary facts usually at the end)
    uniq = list(dict.fromkeys(numbers))

    # Дни месяца и множитель процента пересказывают расчёт, а не его результат.
    constants = set(_claim_numbers(case.get("question") or "")) | CALCULATION_CONSTANTS
    claims = list(dict.fromkeys(_measurements(text)))

    issues = []
    for claim, policy in claims:
        if claim in constants or claim in uniq:
            continue

        exponent = claim.as_tuple().exponent
        if not isinstance(exponent, int) or exponent > 0:
            exponent = 0
        quantum = Decimal(1).scaleb(exponent)

        def _matches(val: Decimal, claim=claim, quantum=quantum) -> bool:
            try:
                return claim == val.quantize(quantum, rounding=ROUND_HALF_UP)
            except InvalidOperation:
                return False

        # Direct rounding check
        found = False
        for n in uniq:
            if _matches(n) or (policy == "percent" and abs(n) <= 1 and _matches(n * 100)):
                found = True
                break

        if found:
            continue

        # A ratio cannot justify an unrelated count just because the numbers
        # happen to divide to it. Derivations require an explicit unit/context.
        if policy == "value":
            issues.append(
                f"Number {format(claim, 'f')} in the answer is not backed by tool results"
            )
            continue

        # Check derived pairs (limit to 500 to avoid CPU spin/OOM)
        # Ratios usually come from aggregate counts/sums which are added at the end (summary facts)
        subset = uniq if len(uniq) <= 500 else (uniq[:300] + uniq[-200:])
        for right in subset:
            if right == 0:
                continue
            for left in subset:
                if (policy == "ratio" and _matches(left / right)) or (
                    policy == "percent"
                    and (_matches(left / right * 100) or _matches((left - right) / right * 100))
                ):
                    found = True
                    break
            if found:
                break

        if not found:
            claim_str = format(claim, "f")
            issues.append(f"Number {claim_str} in the answer is not backed by tool results")

    return issues


def _missing_answer_values(case: dict, blocks: Sequence[Any], text: str) -> list[str]:
    """Reference numbers must reach the answer: a confirmed tool result alone is not an answer.

    Small integers are skipped because prose may spell them out, and charts or dashboards
    deliver their values outside the text.
    """
    expect = case.get("expect") or {}
    if not (expect.get("answer_contains_value") or {}).get("from_result"):
        return []
    if any(_field(block, "type") in {"chart", "dashboard_link", "table"} for block in blocks):
        return []
    claims = _claim_numbers(text)
    issues = []
    for fact in expect.get("facts") or []:
        value = _decimal(fact["value"])
        if value is None or abs(value) < 10 and value == value.to_integral_value():
            continue
        tolerance = _tolerance(fact)
        if not any(
            abs(claim - value) <= tolerance
            or claim == value.quantize(Decimal(1).scaleb(min(0, claim.as_tuple().exponent)))
            for claim in claims
        ):
            issues.append(f"Answer text does not state '{fact['key']}' ({fact['value']})")
    return issues


def _required_answer_rows(case: dict, text: str, results: Sequence[Any]) -> list[str]:
    """Check labelled answers against references, preserving group/value association.

    A table or chart block does not exempt these explicit text requirements. Units
    may be stated in a table header; each group's values must occur in its own
    line/sentence. This is a numeric check, not a judgement of the explanation.
    """
    expect = case.get("expect") or {}
    issues = []
    plain = text.replace("*", "").replace("`", "")
    for pattern in expect.get("answer_units") or []:
        if not re.search(pattern, plain, re.I):
            issues.append(f"Answer does not state the required unit: {pattern}")
    # Decimal separators must not split sentences.
    lines = re.split(
        r"\n|;|(?<![\d)])(?<!шт)(?<!руб)(?<!тыс)(?<!млн)(?<![Сс]р)[.!?]\s+|,\s+(?=[^,:;\n]+:)",
        plain,
    )
    unit_patterns = {
        "hours": r"(?:ч(?:ас\w*)?\.?|hours?|h)\b",
        "seconds": r"(?:сек\w*|seconds?|secs?|s)\b",
        "percent": r"(?:%|процент\w*|percent\w*)",
        "rub": r"(?:руб\w*\.?|RUB|₽)",
        "attempts": r"(?:попыт\w*|attempts?)\b",
    }

    def stated_unit(suffix: str) -> str | None:
        for unit, pattern in unit_patterns.items():
            if re.match(r"\s*" + pattern, suffix, re.I):
                return unit
        return None

    def unit_before(prefix: str) -> str | None:
        # «среднее число попыток 2,28»: единица названа прямо перед числом.
        for unit, pattern in unit_patterns.items():
            if re.search(pattern + r"\s*:?\s*$", prefix, re.I):
                return unit
        return None

    table_headers: dict[str, dict[int, str]] = {}
    header: dict[int, str] = {}
    row_labels = [
        label
        for row in expect.get("answer_rows") or []
        for label in [row["label"], *row.get("aliases", [])]
    ]
    for line in lines:
        if "|" not in line:
            header = {}
            continue
        cells = line.split("|")
        if not any(
            re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", line, re.I) for label in row_labels
        ):
            found = {}
            for index, cell in enumerate(cells):
                for unit, pattern in unit_patterns.items():
                    if re.search(pattern, cell, re.I):
                        found[index] = unit
                        break
            if found:
                header = found
        table_headers[line] = dict(header)
    for reference in expect.get("answer_rows") or []:
        labels = [reference["label"], *reference.get("aliases", [])]

        def labelled(line: str, labels=labels) -> bool:
            return any(
                re.search(r"(?<!\w)" + re.escape(label) + r"\w{0,2}(?!\w)", line, re.I)
                for label in labels
            )

        def has_values(values: Sequence[Any], required=reference["values"]) -> bool:
            numbers = []
            for value in values:
                number = _decimal(value)
                if isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value):
                    number = Decimal(value.replace(",", "."))
                if number is not None:
                    numbers.append(number)
            return all(
                any(
                    abs(number - Decimal(str(wanted["value"])))
                    <= Decimal(str(wanted.get("abs_tolerance", 0)))
                    # Результат может хранить долю, а эталон задан в процентах.
                    or (
                        wanted.get("unit") == "percent"
                        and abs(number * 100 - Decimal(str(wanted["value"])))
                        <= Decimal(str(wanted.get("abs_tolerance", 0)))
                    )
                    for number in numbers
                )
                for wanted in required
            )

        def states_values(line: str, required=reference["values"]) -> bool:
            if not has_values(list(_text_numbers(line))):
                return False
            for wanted in required:
                unit = wanted.get("unit")
                if not unit:
                    continue
                found = False
                for match in NUMBER_RE.finditer(line):
                    numbers = list(_text_numbers(match[0]))
                    if not numbers or abs(numbers[0] - Decimal(str(wanted["value"]))) > Decimal(
                        str(wanted.get("abs_tolerance", 0))
                    ):
                        continue
                    explicit = stated_unit(line[match.end() :]) or unit_before(
                        line[: match.start()]
                    )
                    # Units may be inherited from a table header, but an explicit
                    # contradictory suffix always wins over unrelated text.
                    inherited = table_headers.get(line, {}).get(line[: match.start()].count("|"))
                    if explicit == unit or explicit is None and inherited == unit:
                        found = True
                if not found:
                    return False
            return True

        # Единственную группу и её значения можно назвать в соседних предложениях,
        # если следующее не подписано другой группой.
        next_sentence = len(expect.get("answer_rows") or []) == 1 and any(
            labelled(line) and ":" not in following and states_values(following)
            for line, following in zip(lines, lines[1:], strict=False)
        )
        if not next_sentence and not any(labelled(line) and states_values(line) for line in lines):
            issues.append(f"Answer is missing or misstates values for '{reference['label']}'")
        if not any(
            any(labelled(str(cell)) for cell in row) and has_values(row)
            for group in _structured_groups(results)
            for row in group
            if isinstance(row, (list, tuple))
        ):
            issues.append(f"Tool results do not confirm the row for '{reference['label']}'")
    return issues


def _duration_result_units(case: dict, results: Sequence[ToolResult]) -> list[ToolResult]:
    """Expose independently converted hours for explicitly named duration columns.

    Keep original cells as evidence too. No unit is inferred from the magnitude
    or an ambiguous SQL alias such as ``value``. Used only by opted-in cases.
    """
    if (case.get("expect") or {}).get("result_duration_unit") != "hours":
        return list(results)
    normalized = []
    for result in results:
        data = result.data
        names = _column_names(data)
        conversions = []
        for index, name in enumerate(names):
            if re.search(r"(?:^|_)(?:secs?|seconds?)$", name, re.I):
                conversions.append((index, Decimal(3600)))
            elif re.search(r"(?:^|_)(?:mins?|minutes?)$", name, re.I):
                conversions.append((index, Decimal(60)))
        if data is None or not conversions:
            normalized.append(result)
            continue
        rows = []
        for row in data.rows:
            converted = []
            for index, divisor in conversions:
                try:
                    value = Decimal(str(row[index])) / divisor
                    converted.append(float(value) if value.is_finite() else None)
                except (InvalidOperation, IndexError):
                    converted.append(None)
            rows.append([*row, *converted])
        normalized.append(
            result.model_copy(
                update={
                    "data": data.model_copy(
                        update={
                            "columns": [
                                *data.columns,
                                *(f"{names[index]}_as_hours" for index, _ in conversions),
                            ],
                            "rows": rows,
                        }
                    )
                }
            )
        )
    return normalized


def _verified_percentage(
    fact: dict,
    results: Sequence[Any],
    reference_facts: Sequence[dict],
    column_aliases: dict[str, str] | None,
) -> dict | None:
    """Closed set of golden formulas, using only independently confirmed inputs."""
    sources = {
        "july.change_percent": ["july.counts"],
        "production.error_percent": ["production.error_count", "production.row_count"],
    }.get(fact["key"])
    if sources is None or fact["comparison"] != "approx":
        return None
    references = {item["key"]: item for item in reference_facts}
    if any(
        key not in references
        or not check_fact(references[key], results, column_aliases=column_aliases)
        for key in sources
    ):
        return None
    if fact["key"] == "july.change_percent":
        rows = references["july.counts"]["value"]
        if (
            not isinstance(rows, list)
            or len(rows) != 2
            or any(
                not isinstance(row, list)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not re.fullmatch(r"\d{4}-07", row[0])
                for row in rows
            )
            or rows[0][0] >= rows[1][0]
        ):
            return None
        old, new = (_decimal(row[1]) for row in rows)
        if old is None or new is None or old <= 0 or new < 0:
            return None
        value = (new - old) / old * 100
        formula = "100 * (new - old) / old"
    else:
        errors, total = (_decimal(references[key]["value"]) for key in sources)
        if errors is None or total is None or total <= 0 or not 0 <= errors <= total:
            return None
        value = errors / total * 100
        formula = "100 * errors / total"
    return {"sources": sources, "formula": formula, "value": str(value)}


def _percentage_in_answer(text: str, target: Decimal, tolerance: Decimal) -> bool:
    for claim, policy in _measurements(text):
        if policy != "percent":
            continue
        if abs(claim - target) <= tolerance:
            return True
        try:
            quantum = Decimal(1).scaleb(min(0, claim.as_tuple().exponent))
            if claim == target.quantize(quantum, rounding=ROUND_HALF_UP):
                return True
        except InvalidOperation:
            continue
    return False


def check_fact(
    fact: dict,
    results: Sequence[Any],
    *,
    column_aliases: dict[str, str] | None = None,
    reference_facts: Sequence[dict] = (),
) -> bool:
    """Check one reference fact against tool results only; prose is not evidence."""
    expected = fact["value"]
    comparison = fact["comparison"]

    if comparison == "approx":
        target = _decimal(expected)
        if target is None:
            return False
        tolerance = _tolerance(fact)
        if any(abs(number - target) <= tolerance for number in _structured_numbers(results)):
            return True
        derived = _verified_percentage(fact, results, reference_facts, column_aliases)
        return derived is not None and abs(Decimal(derived["value"]) - target) <= tolerance

    if isinstance(expected, list):
        return collection_matches(fact, _structured_groups(results), column_aliases=column_aliases)

    if comparison == "exact_unordered":
        return False
    if fact["key"] == "dataset.relation_count" and expected == 0:
        for result in _ok_results(results):
            if _field(result, "tool") != "describe_dataset":
                continue
            for values in _data_facts(result):
                tables = values.get("tables")
                if (
                    type(values.get("table_count")) is int
                    and values["table_count"] == 1
                    and isinstance(tables, list)
                    and len(tables) == 1
                ):
                    return True  # One table cannot have inter-table relations.
    return any(_scalar_matches(expected, value) for value in _structured_scalars(results))


def _terminal_issues(
    blocks: Sequence[Any],
    results: Sequence[Any],
    plan: Sequence[Any],
    terminal_error: str | None = None,
) -> list[str]:
    """Terminal API, verification and tool failures invalidate any outcome."""
    issues = []
    if terminal_error:
        issues.append(f"Terminal model error before answer verification: {terminal_error}")
    for block in blocks:
        if _field(block, "type") != "error":
            continue
        markdown = _field(block, "markdown") or ""
        if markdown.startswith(TERMINAL_ERROR_MARKERS):
            issues.append(f"Terminal error before answer verification: {markdown.strip()}")

    batch = list(results)[-len(plan) :] if plan else list(results)
    for result in batch:
        if _field(result, "status") != "error":
            continue
        error = _field(result, "error")
        message = _field(error, "message", "unknown error") if error else "unknown error"
        issues.append(f"Tool '{_field(result, 'tool')}' failed: {message}")
    return issues


def _intent(result: dict) -> str | None:
    understanding = result.get("understanding")
    if understanding is None:
        return None
    return _field(understanding, "intent")


def _block_text(blocks: Sequence[Any]) -> str:
    return " ".join(text for block in blocks if (text := _field(block, "markdown")))


def _data_claim_text(blocks: Sequence[Any], snapshot: Any) -> str:
    """Exclude verified service counts, preserving the title and analytical prose."""
    try:
        saved = DashboardSnapshot.model_validate(snapshot)
    except (ValueError, TypeError):
        return _block_text(blocks)
    if not any(
        _field(block, "type") == "dashboard_link"
        and _field(block, "dashboard_id") == saved.dashboard_id
        for block in blocks
    ):
        return _block_text(blocks)

    # Reproduce the application's own summary from the saved snapshot. Merely
    # mentioning widgets must not whitelist the same number elsewhere in prose.
    summaries = [
        dashboard_preview_blocks(saved)[0].markdown,
        dashboard_refresh_blocks(saved)[0].markdown,
    ]
    parts = []
    for block in blocks:
        text = _field(block, "markdown") or ""
        if _field(block, "type") == "text":
            for summary in summaries:
                if summary and text.startswith(summary):
                    text = saved.title + " " + text[len(summary) :]
                    break
        if text:
            parts.append(text)
    return " ".join(parts)


def _plain(value: Any) -> Any:
    """Snapshot graph data, excluding the client/configuration and provider response."""
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return _plain(value.model_dump(mode="python"))
    if hasattr(value, "tolist") and callable(value.tolist):
        return _plain(value.tolist())
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return float(value)
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def _shown_series(chart: Any, data: Any, col_names: list[str]) -> dict[str, Any]:
    """Only the columns the chart draws: a right number in a neighbouring column proves nothing."""
    fields = [
        _field(_field(chart, "x"), "field") if _field(chart, "x") is not None else None,
        _field(_field(chart, "color"), "field") if _field(chart, "color") is not None else None,
        *(_field(measure, "field") for measure in _field(chart, "y") or []),
    ]
    indexes = [col_names.index(field) for field in fields if field in col_names]
    rows = [[row[i] for i in indexes if i < len(row)] for row in _field(data, "rows") or []]
    return {"columns": [col_names[i] for i in indexes], "rows": rows}


def _check_chart(
    case: dict,
    blocks: Sequence[Any],
    result: dict,
    results: Sequence[Any],
    *,
    context_results: Sequence[dict] | None = None,
    column_aliases: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    expect = case.get("expect") or {}
    chart_expect = expect.get("chart")
    blocks_include = expect.get("blocks_include") or []

    if chart_expect is None and "chart" not in blocks_include:
        return [], {}

    chart_blocks = [
        _field(b, "chart")
        for b in blocks
        if _field(b, "type") == "chart" and _field(b, "chart") is not None
    ]
    if not chart_blocks and isinstance(result, dict) and result.get("charts"):
        chart_blocks = list(result["charts"])

    issues: list[str] = []
    check_details: dict[str, Any] = {
        "found": len(chart_blocks) > 0,
        "charts_count": len(chart_blocks),
        "charts": [],
    }
    if not chart_blocks:
        issues.append("Expected a chart block in the answer, but none was found")
        return issues, check_details

    allowed_types = (chart_expect or {}).get("types_any") or []
    expected_x_kind = (chart_expect or {}).get("x_kind")
    min_points = (chart_expect or {}).get("min_points")
    data_fact_key = (chart_expect or {}).get("data_fact")
    reuse_from = expect.get("reuse_result_from")

    all_available = list(results)
    for cr in context_results or []:
        all_available.extend(_field(cr, "tool_results") or [])

    # Validate all charts in the answer
    for idx, chart in enumerate(chart_blocks):
        c_details: dict[str, Any] = {}
        chart_type = _field(chart, "type")
        c_details["type"] = chart_type

        prefix = f"Chart [{idx}] " if len(chart_blocks) > 1 else "Chart "

        if allowed_types and chart_type not in allowed_types:
            issues.append(f"{prefix}type '{chart_type}' is not in expected types {allowed_types}")

        result_id = _field(chart, "result_id")
        c_details["result_id"] = result_id
        if not result_id:
            issues.append(f"{prefix}does not contain a result_id")
            check_details["charts"].append(c_details)
            continue

        if reuse_from:
            prereq_res_ids = {
                _field(tr, "result_id")
                for cr in (context_results or [])
                if _field(cr, "id") == reuse_from
                for tr in (_field(cr, "tool_results") or [])
            }
            if not prereq_res_ids:
                issues.append(
                    f"{prefix}cannot verify reuse: prerequisite '{reuse_from}' "
                    f"has no tool results or was not found in context"
                )
            elif result_id not in prereq_res_ids:
                issues.append(
                    f"{prefix}result_id '{result_id}' "
                    f"was not reused from prerequisite '{reuse_from}'"
                )

        backing = next((r for r in all_available if _field(r, "result_id") == result_id), None)
        if backing is None:
            issues.append(f"{prefix}result_id '{result_id}' was not found in tool results")
            check_details["charts"].append(c_details)
            continue

        status = _field(backing, "status")
        if status not in ("ok", "empty"):
            issues.append(f"{prefix}backing result '{result_id}' has error status '{status}'")
            check_details["charts"].append(c_details)
            continue

        data = _field(backing, "data")
        columns_info = _field(data, "columns") or []
        col_names = [
            _field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)
            for c in columns_info
        ]
        c_details["columns"] = col_names

        # Missing column metadata is not accepted as passing
        if not col_names and status == "ok":
            issues.append(
                f"{prefix}backing result '{result_id}' has no column metadata to verify axes"
            )

        chart_x = _field(chart, "x")
        if expected_x_kind is not None:
            if chart_x is None:
                issues.append(
                    f"{prefix}missing X axis configuration; expected kind '{expected_x_kind}'"
                )
            elif _field(chart_x, "kind") != expected_x_kind:
                issues.append(
                    f"{prefix}X axis kind '{_field(chart_x, 'kind')}' "
                    f"does not match expected '{expected_x_kind}'"
                )

        if chart_x is not None:
            x_field = _field(chart_x, "field")
            if col_names and x_field not in col_names:
                issues.append(
                    f"{prefix}X axis field '{x_field}' not in backing result columns {col_names}"
                )

        y_measures = _field(chart, "y") or []
        if not y_measures and chart_type != "kpi":
            issues.append(f"{prefix}must specify at least one Y measure")
        for ym in y_measures:
            yf = _field(ym, "field")
            if col_names and yf not in col_names:
                issues.append(
                    f"{prefix}Y measure field '{yf}' not in backing result columns {col_names}"
                )

        if min_points is not None:
            limit = _field(chart, "limit")
            if limit is not None and limit < min_points:
                issues.append(
                    f"{prefix}limit {limit} truncates series below required {min_points} points"
                )
            rows = _field(data, "rows") or []
            if len(rows) < min_points:
                issues.append(
                    f"{prefix}backing result has {len(rows)} data rows, "
                    f"fewer than required {min_points} points"
                )
            else:
                for ym in y_measures:
                    yf = _field(ym, "field")
                    if col_names and yf in col_names:
                        y_idx = col_names.index(yf)
                        valid_pts = sum(
                            1 for r in rows if len(r) > y_idx and _is_finite_number(r[y_idx])
                        )
                        if valid_pts < min_points:
                            issues.append(
                                f"{prefix}backing result has only {valid_pts} valid points "
                                f"for measure '{yf}', fewer than required {min_points} points"
                            )

        if data_fact_key is not None:
            fact = next(
                (f for f in (expect.get("facts") or []) if f.get("key") == data_fact_key), None
            )
            if fact is None:
                issues.append(
                    f"{prefix}expected data_fact '{data_fact_key}' not defined in case facts"
                )
            else:
                shown = _shown_series(chart, data, col_names)
                c_details["data_fact_columns"] = _field(shown, "columns") or []
                if not check_fact(
                    fact, [{"status": status, "data": shown}], column_aliases=column_aliases
                ):
                    issues.append(
                        f"{prefix}displayed values of the chart axes do not match "
                        f"expected fact '{data_fact_key}'"
                    )

        check_details["charts"].append(c_details)

    check_details["passed"] = len(issues) == 0
    return issues, check_details


def _check_dashboard(
    case: dict,
    blocks: Sequence[Any],
    result: dict,
    results: Sequence[Any],
    *,
    dataset: Any = None,
    context_results: Sequence[dict] | None = None,
    column_aliases: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    expect = case.get("expect") or {}
    dash_expect = expect.get("dashboard")
    blocks_include = expect.get("blocks_include") or []
    if dash_expect is None and "dashboard_link" not in blocks_include:
        return [], {}

    issues: list[str] = []
    check_details: dict[str, Any] = {}

    link_block = next((b for b in blocks if _field(b, "type") == "dashboard_link"), None)
    if link_block is None:
        issues.append("Dashboard outcome produced no dashboard_link block")
        return issues, check_details
    dashboard_id = _field(link_block, "dashboard_id")
    if not dashboard_id:
        issues.append("dashboard_link block has no dashboard_id")
        return issues, check_details
    check_details["dashboard_id"] = dashboard_id

    snapshot = result.get("dashboard_snapshot")
    if snapshot is None:
        issues.append("Dashboard snapshot is missing")
        return issues, check_details

    actual_widgets = _field(snapshot, "widgets") or []
    check_details["widget_count"] = len(actual_widgets)
    check_details["widget_ids"] = sorted(
        widget_id for widget in actual_widgets if (widget_id := _field(widget, "widget_id"))
    )
    if len(actual_widgets) < 3 or len(actual_widgets) > 8:
        issues.append(f"Dashboard has {len(actual_widgets)} widgets; expected between 3 and 8")

    reuse_dashboard = expect.get("reuse_dashboard_from")
    if reuse_dashboard:
        prior = next(
            (cr for cr in context_results or [] if _field(cr, "id") == reuse_dashboard),
            None,
        )
        checks = _field(prior, "dashboard_checks") if prior is not None else None
        prior_id = _field(checks, "dashboard_id")
        if prior_id is None:
            issues.append(
                f"Cannot verify dashboard reuse: prerequisite '{reuse_dashboard}' "
                "has no saved dashboard"
            )
        elif dashboard_id != prior_id:
            issues.append(
                f"dashboard_link '{dashboard_id}' does not reuse prerequisite "
                f"dashboard '{prior_id}'"
            )
        prior_widgets = set(_field(checks, "widget_ids") or [])
        if prior_widgets and prior_widgets != set(check_details["widget_ids"]):
            issues.append("Refreshed dashboard does not keep the widgets of the reused dashboard")

    # Recompute the widgets before matching them. A chat refresh creates new result
    # ids that are not part of this turn's `results`; the recipe replay is the only
    # independent backing available to the runner.
    recompute = getattr(dataset, "recompute_widget", None)
    refreshed_results: list[Any] = []
    refreshed_by_widget: dict[Any, Any] = {}
    if recompute is not None:
        for aw in actual_widgets:
            wid = _field(aw, "widget_id")
            try:
                refreshed = recompute(aw)
            except Exception as exc:  # noqa: BLE001 - a broken recipe is the finding
                issues.append(f"Widget '{wid}' refresh failed: {exc}")
                continue
            refreshed_results.append(refreshed)
            if wid:
                refreshed_by_widget[wid] = refreshed

    all_available = [*results, *refreshed_results]
    for cr in context_results or []:
        all_available.extend(_field(cr, "tool_results") or [])

    expected_widgets = (dash_expect or {}).get("widgets") or []
    matched_widget_ids: set[str] = set()
    widget_facts: dict[Any, list[dict]] = {}
    for exp_w in expected_widgets:
        wid = exp_w.get("id")
        exp_types = exp_w.get("chart_types_any") or []
        exp_facts = exp_w.get("facts") or []
        matched = False
        exact_match = next(
            (aw for aw in actual_widgets if wid and _field(aw, "widget_id") == wid),
            None,
        )
        candidates = [exact_match] if exact_match is not None else actual_widgets
        for aw in candidates:
            aw_wid = _field(aw, "widget_id") or str(id(aw))
            if aw_wid in matched_widget_ids:
                continue
            aw_chart = _field(aw, "chart")
            aw_type = _field(aw_chart, "type") if aw_chart else None
            if exp_types and aw_type not in exp_types:
                continue
            aw_status = _field(aw, "status")
            if aw_status not in ("ok", "empty"):
                continue
            aw_res_id = _field(aw, "result_id")
            if not aw_res_id:
                continue
            aw_res = next((r for r in all_available if _field(r, "result_id") == aw_res_id), None)
            if aw_res is None:
                aw_res = refreshed_by_widget.get(aw_wid)
            if aw_res is None or _field(aw_res, "status") not in ("ok", "empty"):
                continue
            if exp_facts:
                needed_facts = [f for f in (expect.get("facts") or []) if f.get("key") in exp_facts]
                if needed_facts and all(
                    check_fact(f, [aw_res], column_aliases=column_aliases) for f in needed_facts
                ):
                    matched = True
                    matched_widget_ids.add(aw_wid)
                    widget_facts[_field(aw, "widget_id")] = needed_facts
                    break
            else:
                matched = True
                matched_widget_ids.add(aw_wid)
                break
        if not matched:
            issues.append(
                f"Dashboard widget '{wid}' (types={exp_types}, facts={exp_facts}) was not satisfied"
            )

    # The runner has no application database: restart and foreign-owner access stay
    # with tests/integration/test_dashboard_chat.py and test_owner_isolation.py.
    # Widget facts are checked against the whole result, not the axes its chart draws.
    check_details["not_evaluated"] = ["restart", "foreign_owner", "widget_chart_axes"]
    check_details["recomputed_tools"] = sorted(
        {str(tool) for result in refreshed_results if (tool := _field(result, "tool"))}
    )
    check_details["_refreshed_results"] = refreshed_results

    if recompute is None:
        check_details["not_evaluated"].append("refresh")
    else:
        for aw in actual_widgets:
            wid = _field(aw, "widget_id")
            refreshed = refreshed_by_widget.get(wid)
            if refreshed is None:
                continue  # the recipe failure is already reported above
            if _field(refreshed, "status") != "ok":
                issues.append(f"Widget '{wid}' refresh returned '{_field(refreshed, 'status')}'")
            if _field(refreshed, "result_id") == _field(aw, "result_id"):
                issues.append(f"Widget '{wid}' refresh did not produce a new result_id")
            for fact in widget_facts.get(wid, []):
                if not check_fact(fact, [refreshed], column_aliases=column_aliases):
                    issues.append(f"Widget '{wid}' refresh does not confirm fact '{fact['key']}'")

    check_details["passed"] = len(issues) == 0
    return issues, check_details


def _column_names(data: Any) -> list[str]:
    return [
        _field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)
        for c in (_field(data, "columns") or [])
    ]


def _check_correlation_results(case: dict, results: Sequence[Any]) -> list[str]:
    """Check correlation tools by their own columns; other tools are covered by facts."""
    expect = case.get("expect") or {}
    facts = {fact["key"]: fact for fact in expect.get("facts") or []}
    issues: list[str] = []
    for result in _ok_results(results):
        tool = _field(result, "tool")
        if tool not in {"correlation", "lagged_correlation"}:
            continue
        data = _field(result, "data")
        names = _column_names(data)
        rows = _field(data, "rows") or []
        if "r" not in names:
            issues.append(f"Tool '{tool}' result has no 'r' column")
            continue
        r_index = names.index("r")
        for row in rows:
            if not _is_finite_number(row[r_index]) or not -1 <= float(row[r_index]) <= 1:
                issues.append(f"Tool '{tool}' produced invalid Pearson r: {row[r_index]}")
        if tool == "correlation" and expect.get("population") == "complete_measure_pairs":
            n_index = names.index("n") if "n" in names else None
            if n_index is None or not any(row[n_index] == 500000 for row in rows):
                issues.append("Correlation was not computed over 500000 complete pairs")
        if tool == "lagged_correlation" and "lag" in names:
            by_lag = {row[names.index("lag")]: row[r_index] for row in rows}
            for lag, key in (
                (5, "lag.plus5_r"),
                (facts.get("lag.best_days", {}).get("value"), "lag.best_r"),
            ):
                fact = facts.get(key)
                if fact is None:
                    continue
                actual = _decimal(by_lag.get(lag))
                if actual is None or abs(actual - _decimal(fact["value"])) > _tolerance(fact):
                    issues.append(f"Lag {lag} does not have the expected r for '{key}': {actual}")
    return issues


def _holdout_leak_rubric(case: dict, results: Sequence[Any]) -> tuple[str, str | None]:
    """Structured evidence only: the confirmed reference table separates forecast from actual."""
    fact = next(
        (
            f
            for f in (case.get("expect") or {}).get("facts") or []
            if f["key"] == "forecast.holdout7"
        ),
        None,
    )
    if fact is not None and check_fact(fact, results):
        return "passed", None
    for result in _ok_results(results):
        data = _field(result, "data")
        names = [str(name).lower() for name in _column_names(data)]
        forecast = next(
            (
                i
                for i, c in enumerate(names)
                if any(k in c for k in ("forecast", "прогноз", "yhat"))
            ),
            None,
        )
        actual = next(
            (i for i, c in enumerate(names) if any(k in c for k in ("actual", "fact", "факт"))),
            None,
        )
        rows = _field(data, "rows") or []
        if forecast is None or actual is None or len(rows) < 3:
            continue
        if sum(row[forecast] == row[actual] for row in rows) / len(rows) >= 0.7:
            return "failed", "Forecast values systematically equal holdout actuals"
    return "not_evaluated", None


# Rubrics decided by structured evidence. Prose has no reliable evaluator: keywords
# cannot tell "это не выручка" from "это выручка", so everything else stays not_evaluated.
RUBRIC_EVALUATORS = {
    "Факт holdout использован как его прогноз.": _holdout_leak_rubric,
}


def _truncated_scope_issues(text: str, results: Sequence[Any]) -> list[str]:
    """A claim about the whole data must not be drawn from a result cut by LIMIT."""
    if not SCOPE_CLAIM_RE.search(text):
        return []
    for result in _ok_results(results):
        limit = LIMIT_RE.search(_field(result, "sql") or "")
        rows = _field(_field(result, "data"), "rows") or []
        if limit and len(rows) == int(limit[1]):
            return [
                f"Answer describes the data scope from a result cut by LIMIT {limit[1]}: "
                f"'{SCOPE_CLAIM_RE.search(text)[0]}'"
            ]
    return []


def _unit_claim_issues(case: dict, text: str) -> list[str]:
    """A count of distinct codes or chains must not be presented as a number of rows."""
    plain = text.replace("*", "").replace("`", "")
    issues = []
    for fact in (case.get("expect") or {}).get("facts") or []:
        unit = str(fact.get("unit") or "").lower()
        value = _decimal(fact["value"])
        if value is None or not unit.startswith("разн") or "попыток" in unit:
            continue
        for match in NUMBER_RE.finditer(plain):
            numbers = list(_text_numbers(match[0]))
            if numbers and numbers[0] == value and ROWS_NOUN_RE.match(plain[match.end() :]):
                issues.append(
                    f"Answer calls {format(value, 'f')} rows; fact '{fact['key']}' counts: {unit}"
                )
                break
    return issues


def _weekday_label_issues(case: dict, text: str) -> list[str]:
    """Check "Пятница (4)" pairs when the question fixes the Monday=0 numbering."""
    facts = (case.get("expect") or {}).get("facts") or []
    if not any("понедельник=0" in str(fact.get("unit")) for fact in facts):
        return []
    issues = []
    for name, number in WEEKDAY_LABEL_RE.findall(text):
        expected = next(i for i, stem in enumerate(WEEKDAY_STEMS) if name.lower().startswith(stem))
        if int(number) != expected:
            issues.append(f"Weekday '{name}' is labelled {number}; Monday=0 makes it {expected}")
    return issues


def _evaluate_semantics(case: dict, text: str, results: Sequence[Any]) -> dict[str, Any]:
    """Report rubrics separately; a rubric without honest evidence is not_evaluated."""
    expect = case.get("expect") or {}
    rubrics: list[dict[str, Any]] = []

    def add(kind: str, rubric: Any) -> None:
        evaluator = RUBRIC_EVALUATORS.get(rubric) if isinstance(rubric, str) else None
        status, reason = evaluator(case, results) if evaluator else ("not_evaluated", None)
        rubrics.append({"kind": kind, "rubric": rubric, "status": status, "reason": reason})

    for kind in ("must_explain", "must_not_claim"):
        for rubric in expect.get(kind) or []:
            add(kind, rubric)
    clarification = expect.get("clarification") or {}
    if clarification.get("resolves"):
        add("clarification.resolves", clarification["resolves"])
    if clarification.get("options"):
        add("clarification.options", clarification["options"])

    code_issues = [
        *_truncated_scope_issues(text, results),
        *_unit_claim_issues(case, text),
        *_weekday_label_issues(case, text),
    ]
    statuses = {rubric["status"] for rubric in rubrics}
    if code_issues or "failed" in statuses:
        status = "failed"
    elif statuses <= {"passed"}:  # A case without rubrics has nothing left to review.
        status = "passed"
    elif "passed" in statuses:
        status = "partial"
    else:
        status = "not_evaluated"
    issues = [
        *code_issues,
        *(
            f"Rubric '{rubric['rubric']}' failed: {rubric['reason']}"
            for rubric in rubrics
            if rubric["status"] == "failed"
        ),
    ]
    return {"status": status, "rubrics": rubrics, "issues": issues}


def evaluate_case(
    case: dict,
    result: dict,
    *,
    column_aliases: dict[str, str] | None = None,
    dataset: Any = None,
    context_results: Sequence[dict] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Score one graph result; terminal failures are rejected before facts/intent."""
    expect = case.get("expect", {})
    outcome = expect.get("outcome")
    blocks = list(result.get("answer_blocks") or [])
    results = [item for item in (result.get("results") or []) if isinstance(item, ToolResult)]
    plan = list(result.get("plan") or [])
    text = _block_text(blocks)
    claim_text = _data_claim_text(blocks, result.get("dashboard_snapshot"))
    used_tools = [tool for item in results if (tool := _field(item, "tool"))]
    reused_results, reuse_source, reuse_issues = _reused_results(expect, context_results)
    evidence_results = [*results, *reused_results]
    reused_tools = [tool for item in reused_results if (tool := _field(item, "tool"))]
    forbidden = set(expect.get("tools_forbidden") or [])
    if {"run_sql", "describe_dataset", "describe_column", "value_counts"} <= forbidden:
        forbidden |= SUPPORTED_TOOLS
    called_forbidden = sorted(forbidden.intersection(used_tools))
    planned_forbidden = sorted(forbidden.intersection(_field(step, "tool") for step in plan))
    details: dict[str, Any] = {
        "answer": text,
        "used_tools": used_tools,
        "fact_checks": [],
        "plan": _plain(plan),
        "tool_results": _plain(results),
        "answer_blocks": _plain(blocks),
        "intent": _intent(result),
        "understanding": _plain(result.get("understanding")),
        "verification": _plain(result.get("verification")),
        "safety_checks": {
            "forbidden_tools": sorted(forbidden),
            "called_forbidden": called_forbidden,
            "planned_forbidden": planned_forbidden,
            "passed": not (called_forbidden or planned_forbidden),
        },
        "diagnostics": result.get("_eval_diagnostics", []),
        "terminal_error": result.get("terminal_error"),
    }

    if reuse_source is not None:
        details["reuse"] = {
            "from": reuse_source,
            "result_ids": [
                result_id
                for result_id in (_field(item, "result_id") for item in reused_results)
                if result_id
            ],
            "tools": list(dict.fromkeys(reused_tools)),
        }
    terminal_issues = _terminal_issues(blocks, results, plan, result.get("terminal_error"))
    automated_issues = [*terminal_issues, *reuse_issues]
    if terminal_issues:
        semantics = _evaluate_semantics(case, "", [])
        details["automated_status"] = "failed"
        details["automated_issues"] = automated_issues
        details["semantic_status"] = semantics["status"]
        details["semantic_issues"] = semantics["issues"]
        details["semantic_rubrics"] = semantics["rubrics"]
        return "failed", automated_issues, details

    if called_forbidden or planned_forbidden:
        violations = sorted(set(called_forbidden + planned_forbidden))
        automated_issues.append(f"Used or planned forbidden tools: {violations}")

    # Check required blocks
    for bt in expect.get("blocks_include") or []:
        if not any(_field(b, "type") == bt for b in blocks):
            automated_issues.append(
                f"Expected block of type '{bt}' in answer blocks, but none found"
            )

    if outcome == "answer":
        tools_any = set(expect.get("tools_any") or []) | (
            set(expect.get("tools_planned") or []) & SUPPORTED_TOOLS
        )
        if not any(
            _field(block, "type") == "text" and (_field(block, "markdown") or "").strip()
            for block in blocks
        ):
            automated_issues.append("The answer contains no text block")

        results = _duration_result_units(case, results)
        evidence_results = [*results, *reused_results]
        # A dashboard refresh has no ordinary tool results: its widget recipes are
        # replayed by the dashboard check, and those results become the evidence.
        dash_issues, dash_details = _check_dashboard(
            case,
            blocks,
            result,
            results,
            dataset=dataset,
            context_results=context_results,
            column_aliases=column_aliases,
        )
        dashboard_evidence = list(dash_details.pop("_refreshed_results", []))
        automated_issues.extend(dash_issues)
        if dash_details:
            details["dashboard_checks"] = dash_details
        reuse_dashboard = expect.get("reuse_dashboard_from")
        if reuse_dashboard:
            evidence_results = [*results, *reused_results, *dashboard_evidence]

        reference_facts = expect.get("facts") or []
        facts_from_context = False
        facts_from_dashboard = False
        for fact in reference_facts:
            result_ok = check_fact(
                fact, results, column_aliases=column_aliases, reference_facts=reference_facts
            )
            fact_source = "current"
            if not result_ok and reused_results:
                result_ok = check_fact(
                    fact,
                    reused_results,
                    column_aliases=column_aliases,
                    reference_facts=reference_facts,
                )
                if result_ok:
                    fact_source = f"context:{reuse_source}"
                    facts_from_context = True
            if not result_ok and dashboard_evidence:
                result_ok = check_fact(
                    fact,
                    dashboard_evidence,
                    column_aliases=column_aliases,
                    reference_facts=reference_facts,
                )
                if result_ok:
                    fact_source = f"dashboard:{dash_details.get('dashboard_id')}"
                    facts_from_dashboard = True
            fact_check = {
                "key": fact["key"],
                "comparison": fact["comparison"],
                "expected": fact["value"],
                "result": result_ok,
                "passed": result_ok,
                "source": fact_source,
            }
            derived = _verified_percentage(fact, evidence_results, reference_facts, column_aliases)
            if derived is not None:
                fact_check["derivation"] = derived
                if not _percentage_in_answer(
                    claim_text, Decimal(derived["value"]), _tolerance(fact)
                ):
                    automated_issues.append(
                        f"Percentage '{fact['key']}' is missing or incorrect in the answer"
                    )
            details["fact_checks"].append(fact_check)
            if not result_ok:
                automated_issues.append(
                    f"Fact '{fact['key']}' (value: {fact['value']}) "
                    "is not confirmed by tool results"
                )

        expected_from_current = set(tools_any).intersection(used_tools)
        expected_from_context = set(tools_any).intersection(reused_tools)
        expected_from_dashboard = set(tools_any).intersection(
            dash_details.get("recomputed_tools") or []
        )
        facts_passed = all(check["passed"] for check in details["fact_checks"])
        # A memory-only answer is accepted only when the declared prerequisite really
        # contains the expected tool and every reference fact is confirmed there.
        memory_satisfies_tools = bool(
            reuse_source and reused_results and expected_from_context and facts_from_context
        )
        # A dashboard refresh runs its recipes inside the backend; the replayed
        # widget results are the equivalent evidence of those tool calls.
        dashboard_satisfies_tools = bool(
            reuse_dashboard
            and dashboard_evidence
            and expected_from_dashboard
            and facts_from_dashboard
        )
        if reuse_source:
            details["reuse"]["accepted"] = bool(memory_satisfies_tools and facts_passed)
        if reuse_dashboard:
            details.setdefault("dashboard_checks", {})["reuse_accepted"] = bool(
                dashboard_satisfies_tools and facts_passed
            )
        if (
            tools_any
            and not expected_from_current
            and not (memory_satisfies_tools and facts_passed)
            and not (dashboard_satisfies_tools and facts_passed)
        ):
            automated_issues.append(
                f"Did not use any expected tools {tools_any}. Used: {used_tools}"
            )
        automated_issues.extend(_answer_issues(case, claim_text, evidence_results))
        automated_issues.extend(_missing_answer_values(case, blocks, claim_text))
        automated_issues.extend(_required_answer_rows(case, claim_text, results))

        # Chart checks
        chart_issues, chart_details = _check_chart(
            case,
            blocks,
            result,
            results,
            context_results=context_results,
            column_aliases=column_aliases,
        )
        automated_issues.extend(chart_issues)
        if chart_details:
            details["chart_checks"] = chart_details

        # Correlation tool checks
        automated_issues.extend(_check_correlation_results(case, evidence_results))

    elif outcome == "clarify":
        intent = _intent(result)
        if intent != expect.get("intent"):
            automated_issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not any(
            _field(block, "type") == "clarification" and (_field(block, "markdown") or "").strip()
            for block in blocks
        ):
            automated_issues.append("Clarification outcome produced no clarification block")
        if expect.get("clarification"):
            clarification = _field(result.get("understanding"), "clarification")
            options = list(_field(clarification, "options", []) or [])
            for block in blocks:
                options.extend(_field(block, "options", []) or [])
            if not options:
                options = re.findall(r"(?m)^\s*[-*]\s+(.+)$", text)
            choices = {
                option.strip() for option in options if isinstance(option, str) and option.strip()
            }
            if len(choices) < 2:
                automated_issues.append("Clarification provides fewer than two distinct options")

    elif outcome == "reject":
        intent = _intent(result)
        if intent != expect.get("intent"):
            automated_issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not text.strip():
            automated_issues.append("Rejection produced no answer blocks")

    else:
        automated_issues.append(f"Unsupported expected outcome '{outcome}'")

    semantics = _evaluate_semantics(case, text, evidence_results)
    automated_passed = not automated_issues

    details["automated_status"] = "passed" if automated_passed else "failed"
    details["automated_issues"] = list(automated_issues)
    details["semantic_status"] = semantics["status"]
    details["semantic_issues"] = semantics["issues"]
    details["semantic_rubrics"] = semantics["rubrics"]

    all_issues = automated_issues + semantics["issues"]
    if all_issues:
        status = "failed"
    elif semantics["status"] == "passed":
        status = "passed"
    else:
        status = "needs_review"  # Automated checks hold; the rubrics still need a person.
    return status, all_issues, details


class EvalDashboardStore:
    """Saved dashboards of one evaluation chat, with an autonomous refresh.

    The real application stores dashboards in its database. The eval runner has no
    application database, but a follow-up may need the dashboard created by its
    prerequisite, so the same contract is kept in memory for one chat.
    """

    def __init__(self, dataset: Any) -> None:
        self.dataset = dataset
        self._snapshots: dict[str, DashboardSnapshot] = {}

    def save(
        self, title: str, description: str | None, widgets: list[WidgetSnapshot]
    ) -> DashboardSnapshot:
        snapshot = DashboardSnapshot(
            dashboard_id=f"d_eval_{uuid4().hex[:12]}",
            dataset_id=self.dataset.catalog.dataset_id,
            title=title,
            description=description,
            widgets=widgets,
            consistency="immutable",
            created_at=datetime.now(UTC),
            refreshed_at=datetime.now(UTC),
        )
        self._snapshots[snapshot.dashboard_id] = snapshot
        return snapshot

    def refresh(self, dashboard_id: str) -> DashboardSnapshot:
        """Recompute every widget recipe and return the same saved dashboard."""
        snapshot = self._snapshots.get(dashboard_id)
        if snapshot is None:
            raise ValueError("Dashboard not found in this chat")
        recompute = getattr(self.dataset, "recompute_widget", None)
        if recompute is None:
            raise ValueError("Dashboard refresh is unavailable")

        widgets: list[WidgetSnapshot] = []
        for widget in snapshot.widgets:
            template = (
                ChartTemplate(**widget.chart.model_dump(exclude={"result_id"}))
                if isinstance(widget.chart, ChartSpec)
                else widget.chart
            )
            result = recompute(widget)
            if result.status not in ("ok", "empty"):
                widgets.append(
                    widget.model_copy(
                        update={
                            "chart": template,
                            "result_id": None,
                            "status": "error",
                            "error": result.error,
                        }
                    )
                )
                continue
            read = result.read
            widgets.append(
                widget.model_copy(
                    update={
                        "chart": chart_for_result(template, result),
                        "result_id": result.result_id,
                        "status": result.status,
                        "error": None,
                        "read_started_at": read.started_at if read is not None else None,
                        "read_finished_at": read.finished_at if read is not None else None,
                    }
                )
            )
        refreshed = snapshot.model_copy(
            update={"widgets": widgets, "refreshed_at": datetime.now(UTC)}
        )
        self._snapshots[dashboard_id] = refreshed
        return refreshed


class GoldenDataset:
    """Pinned golden CSV ingested once into a temporary DuckDB dataset."""

    def __init__(
        self,
        catalog: DatasetCatalog,
        database_path: Path,
        temporary: tempfile.TemporaryDirectory,
    ):
        self._temporary = temporary
        self.catalog = catalog
        self.database_path = database_path
        self.allowed_tables = frozenset(table.table_id for table in catalog.tables)

    def load_catalog(self, dataset_id: str) -> DatasetCatalog:
        if dataset_id != self.catalog.dataset_id:
            raise ValueError(f"Agent requested unknown dataset {dataset_id!r}")
        return self.catalog

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(
            str(self.database_path),
            read_only=True,
            config={"enable_external_access": False},
        )

    def _run_step(
        self,
        connection: duckdb.DuckDBPyConnection,
        tool: str,
        params: dict[str, Any],
        source_result: ToolResult | None,
    ) -> ToolResult:
        allowed_tables = set(self.allowed_tables)
        if source_result is not None and source_result.data is not None:
            frame = pd.DataFrame(source_result.data.rows, columns=source_result.data.columns)
            connection.register("source_result", frame)
            allowed_tables.add("source_result")
        return execute_tool(tool, params, connection=connection, allowed_tables=allowed_tables)

    def dispatch(
        self, tool: str, params: dict[str, Any], source_result: ToolResult | None = None
    ) -> ToolResult:
        with self._connect() as connection:
            return self._run_step(connection, tool, params, source_result)

    def recompute_widget(self, widget: WidgetSnapshot) -> ToolResult:
        """Re-run a saved recipe with the production tools, as dashboard refresh does."""
        with self._connect() as connection:
            if widget.tool_call is not None:
                return self._run_step(
                    connection, widget.tool_call.tool, widget.tool_call.params, None
                )
            results: dict[str, ToolResult] = {}
            for step in widget.recipe.steps:
                parent = results.get(step.source_step_id) if step.source_step_id else None
                results[step.step_id] = self._run_step(
                    connection, step.call.tool, step.call.params, parent
                )
            return results[widget.recipe.output_step_id]

    def close(self) -> None:
        self._temporary.cleanup()


def open_dataset(data_file: Path, manifest: dict[str, Any]) -> GoldenDataset:
    """Load the pinned CSV through the production ingestion path, not a mock DB."""
    temporary = tempfile.TemporaryDirectory(prefix="djedai-eval-")
    database_path = Path(temporary.name) / "data.duckdb"
    try:
        catalog = ingest_csv_dataset(
            [CsvSource(path=data_file, original_name=data_file.name)],
            database_path,
            dataset_id=manifest["dataset_id"],
            name=manifest["source"]["file_name"],
            created_at=datetime.now(UTC),
        )
    except Exception:
        temporary.cleanup()
        raise
    return GoldenDataset(catalog, database_path, temporary)


def _case_skip_reason(case: dict) -> str | None:
    expect = case.get("expect", {})
    requirements = set(expect.get("requirements") or [])
    unsupported = requirements - SUPPORTED_REQUIREMENTS
    if unsupported:
        return f"Unsupported requirements: {sorted(unsupported)}"
    alternatives = set(expect.get("tools_any") or []) | set(expect.get("tools_planned") or [])
    if alternatives and not alternatives.intersection(SUPPORTED_TOOLS):
        return f"Unsupported tools: {sorted(alternatives)}"
    if expect.get("outcome") not in {"answer", "clarify", "reject", "input_error"}:
        return f"Unsupported expected outcome: {expect.get('outcome')!r}"
    if expect.get("outcome") == "input_error":
        return INPUT_ERROR_REASON
    return None


def _dependency_order(case: dict, cases_by_id: dict[str, dict]) -> list[dict]:
    """Resolve context.previous_cases recursively in declaration order, without repeats."""
    order: list[dict] = []
    visiting: set[str] = set()

    def visit(current: dict) -> None:
        context = current.get("context") or {}
        for dependency_id in context.get("previous_cases") or []:
            if dependency_id in visiting:
                raise ValueError(f"Context cycle through {dependency_id!r}")
            dependency = cases_by_id.get(dependency_id)
            if dependency is None:
                raise ValueError(f"Unknown context case {dependency_id!r}")
            visiting.add(dependency_id)
            visit(dependency)
            if dependency not in order:
                order.append(dependency)
        visiting.discard(current.get("id"))

    visit(case)
    return order


class EvalInvocationError(RuntimeError):
    """A failed graph turn with the last checkpoint and content-free diagnostics."""

    def __init__(self, error: Exception, result: dict) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.result = result
        self.error_type = type(error).__name__


def _invoke(
    graph: Any,
    dataset: GoldenDataset | Any,
    client: Any,
    thread_id: str,
    history: Sequence[Message],
    question: str,
    *,
    dashboards: EvalDashboardStore | None = None,
) -> dict:
    state = {
        "chat_id": thread_id,
        "dataset_id": dataset.catalog.dataset_id,
        "messages": [*history, Message(role="user", content=question)],
    }
    events: list[dict] = []
    diagnostics = RunDiagnostics(
        events.append,
        owner_id="eval",
        chat_id=thread_id,
        dataset_id=dataset.catalog.dataset_id,
        user_message_id=f"eval_{uuid4().hex}",
    )
    budget = ExecutionBudget(get_limits().agent_turn_timeout_seconds)

    config = {
        "configurable": {
            "thread_id": thread_id,
            "client": client,
            "catalog_loader": dataset.load_catalog,
            "tool_dispatcher": dataset.dispatch,
            "budget": budget,
            "diagnostics": diagnostics,
        }
    }
    if dashboards is not None:
        config["configurable"]["dashboard_saver"] = dashboards.save
        config["configurable"]["dashboard_refresher"] = dashboards.refresh
    try:
        with budget.activate():
            result = graph.invoke(state, config=config)
    except Exception as error:
        diagnostics.finish("error", type(error).__name__)
        partial = {}
        try:
            checkpoint = graph.get_state(config)
            if isinstance(checkpoint.values, dict):
                partial = checkpoint.values
        except Exception:  # A graph can fail before creating its first checkpoint.
            pass
        raise EvalInvocationError(error, {**partial, "_eval_diagnostics": events}) from error
    terminal = _terminal_issues(
        result.get("answer_blocks", []),
        result.get("results", []),
        result.get("plan", []),
        result.get("terminal_error"),
    )
    diagnostics.finish(
        "error" if terminal else "success",
        (result.get("terminal_error") or diagnostics.last_error) if terminal else None,
    )
    return {**result, "_eval_diagnostics": events}


def process_case(
    case: dict,
    cases_by_id: dict[str, dict],
    client: Any,
    graph: Any,
    dataset: GoldenDataset | Any,
) -> dict[str, Any]:
    """Run prerequisites and the case in one chat thread, then score the case."""
    overall_start = time.perf_counter()
    case_id = case["id"]
    skip_reason = _case_skip_reason(case)
    if skip_reason is not None:
        return {"id": case_id, "status": "skipped", "reason": skip_reason}
    if case.get("dataset") != dataset.catalog.dataset_id:
        return {
            "id": case_id,
            "status": "blocked",
            "reason": "Question dataset differs from the loaded dataset",
        }

    thread_id = f"eval_{case_id}"
    history: list[Message] = []
    executed_context: list[str] = []
    context_results: list[dict] = []
    dashboards = EvalDashboardStore(dataset)
    column_aliases = {
        column.original_name: column.name
        for table in getattr(dataset.catalog, "tables", [])
        for column in table.columns
    }

    try:
        dependencies = _dependency_order(case, cases_by_id)
    except ValueError as error:
        return {
            "id": case_id,
            "status": "blocked",
            "reason": str(error),
            "thread_id": thread_id,
            "context_results": context_results,
        }

    for dependency in dependencies:
        dependency_id = dependency["id"]
        dependency_skip = _case_skip_reason(dependency)
        if dependency.get("dataset") != dataset.catalog.dataset_id:
            dependency_skip = "Question dataset differs from the loaded dataset"
        if dependency_skip is not None:
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} is skipped: {dependency_skip}",
                "thread_id": thread_id,
                "context_cases": executed_context,
                "context_results": context_results,
            }
        executed_context.append(dependency_id)
        dependency_start = time.perf_counter()
        try:
            dependency_result = _invoke(
                graph,
                dataset,
                client,
                thread_id,
                history,
                dependency.get("question", ""),
                dashboards=dashboards,
            )
        except Exception as error:  # noqa: BLE001 - a prerequisite failure blocks the case
            partial = error.result if isinstance(error, EvalInvocationError) else {}
            _, _, details = evaluate_case(
                dependency,
                partial,
                column_aliases=column_aliases,
                dataset=dataset,
            )
            context_results.append(
                {
                    "id": dependency_id,
                    "status": "failed",
                    "automated_status": "failed",
                    "semantic_status": details.get("semantic_status", "failed"),
                    **details,
                    "issues": [f"Graph execution failed: {error}"],
                    "duration_seconds": round(time.perf_counter() - dependency_start, 2),
                }
            )
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} failed to run: {error}",
                "thread_id": thread_id,
                "context_cases": executed_context,
                "context_results": context_results,
            }
        status, issues, details = evaluate_case(
            dependency,
            dependency_result,
            column_aliases=column_aliases,
            dataset=dataset,
        )
        context_results.append(
            {
                "id": dependency_id,
                "status": status,
                "automated_status": details.get("automated_status", status),
                "semantic_status": details.get("semantic_status", status),
                "issues": issues,
                **details,
                "duration_seconds": round(time.perf_counter() - dependency_start, 2),
            }
        )
        # Unreviewed rubrics do not break the chat history; failed automated checks do.
        if details["automated_status"] != "passed":
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} {status}: {'; '.join(issues)}",
                "thread_id": thread_id,
                "context_cases": executed_context,
                "context_results": context_results,
            }
        history.extend(
            [
                Message(role="user", content=dependency.get("question", "")),
                Message(role="assistant", content=details["answer"]),
            ]
        )

    start_time = time.perf_counter()
    try:
        result = _invoke(
            graph,
            dataset,
            client,
            thread_id,
            history,
            case.get("question", ""),
            dashboards=dashboards,
        )
    except Exception as error:  # noqa: BLE001 - report the failed run, not a crash
        partial = error.result if isinstance(error, EvalInvocationError) else {}
        _, _, details = evaluate_case(
            case,
            partial,
            column_aliases=column_aliases,
            dataset=dataset,
            context_results=context_results,
        )
        return {
            **details,
            "id": case_id,
            "status": "failed",
            "automated_status": "failed",  # A partial checkpoint is not a finished turn.
            "semantic_status": details.get("semantic_status", "failed"),
            "duration_seconds": round(time.perf_counter() - start_time, 2),
            "total_duration_seconds": round(time.perf_counter() - overall_start, 2),
            "thread_id": thread_id,
            "context_cases": executed_context,
            "context_results": context_results,
            "error_type": error.error_type
            if isinstance(error, EvalInvocationError)
            else type(error).__name__,
            "issues": [f"Graph execution failed: {error}"],
        }

    duration = time.perf_counter() - start_time
    status, issues, details = evaluate_case(
        case,
        result,
        column_aliases=column_aliases,
        dataset=dataset,
        context_results=context_results,
    )
    return {
        **details,
        "id": case_id,
        "status": status,
        "automated_status": details.get("automated_status", status),
        "semantic_status": details.get("semantic_status", status),
        "duration_seconds": round(duration, 2),
        "total_duration_seconds": round(time.perf_counter() - overall_start, 2),
        "thread_id": thread_id,
        "context_cases": executed_context,
        "context_results": context_results,
        "issues": issues,
    }


def _prepare_dataset(
    data_file: Path | None,
    manifest: dict[str, Any],
    *,
    download: bool,
) -> GoldenDataset:
    if data_file is None:
        raise ValueError(
            "Provide --data-file with the pinned golden CSV (or --download to fetch it)"
        )
    data_file = data_file.expanduser()
    if download:
        download_file(data_file, manifest["source"])
    elif not data_file.exists():
        raise ValueError(f"Golden CSV not found: {data_file}")
    check_identity(data_file, manifest["source"])
    return open_dataset(data_file, manifest)


def run_eval(
    questions_path: Path,
    output_path: Path | None,
    *,
    data_file: Path | None = None,
    manifest_path: Path = DEFAULT_MANIFEST,
    download: bool = False,
    dataset: GoldenDataset | Any | None = None,
    max_workers: int = 5,
) -> dict:
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    with open(questions_path, encoding="utf-8") as f:
        cases = yaml.load(f, Loader=UniqueKeyLoader)
    if not isinstance(cases, list):
        raise ValueError("Questions must be a YAML list")

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases_by_id = {case["id"]: case for case in cases}
    if len(cases_by_id) != len(cases):
        raise ValueError("Question IDs must be unique: chat checkpoints cannot be shared by cases")
    runnable = [case for case in cases if _case_skip_reason(case) is None]

    client = None
    graph = None
    blocked_reason = None
    if runnable:
        try:
            client = get_agent_client()
        except Exception as e:  # noqa: BLE001 - a missing client blocks every case
            blocked_reason = f"Model client unavailable: {e}"
        else:
            graph = build_agent_graph()

    owns_dataset = False
    if runnable and not blocked_reason and dataset is None:
        dataset = _prepare_dataset(data_file, manifest, download=download)
        owns_dataset = True

    report: dict[str, Any] = {
        "schema_version": 2,
        "dataset": manifest.get("dataset_id"),
        "capabilities": {
            "tools": sorted(SUPPORTED_TOOLS),
            "requirements": sorted(SUPPORTED_REQUIREMENTS),
        },
        "evaluation_scope": {
            "semantic_rubrics": "code_checks_only_rest_not_evaluated",
            "transport": "direct_graph",
            "turn_timeout_seconds": get_limits().agent_turn_timeout_seconds,
        },
        "status": "running",
        "started_at": started_at,
        "summary": {
            "total": len(cases),
            "passed": 0,
            "failed": 0,
            "needs_review": 0,
            "skipped": 0,
            "blocked": 0,
            "pending": len(runnable),
            "automated_passed": 0,
            "automated_failed": 0,
            "semantic_passed": 0,
            "semantic_failed": 0,
            "semantic_partial": 0,
            "semantic_not_evaluated": 0,
            "timed_out": 0,
            "rubrics_passed": 0,
            "rubrics_failed": 0,
            "rubrics_not_evaluated": 0,
        },
        "cases": [
            {
                "id": case["id"],
                "question": case.get("question", ""),
                "scenario": case.get("scenario"),
                "status": "pending",
            }
            for case in cases
        ],
    }

    def save() -> None:
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_name(output_path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, ensure_ascii=False, default=str)
            temporary.replace(output_path)

    print(f"Starting evaluation of {len(cases)} cases ({len(runnable)} runnable)...")
    workers = max(1, min(max_workers, len(runnable) or 1))
    try:
        for index, case in enumerate(cases):
            skip_reason = _case_skip_reason(case)
            if skip_reason is not None:
                report["cases"][index] = {
                    "id": case["id"],
                    "question": case.get("question", ""),
                    "scenario": case.get("scenario"),
                    "status": "skipped",
                    "reason": skip_reason,
                }
                report["summary"]["skipped"] += 1
        save()

        if blocked_reason is not None:
            for index, case in enumerate(cases):
                if _case_skip_reason(case) is None:
                    report["cases"][index] = {
                        "id": case["id"],
                        "question": case.get("question", ""),
                        "scenario": case.get("scenario"),
                        "status": "blocked",
                        "reason": blocked_reason,
                    }
                    report["summary"]["blocked"] += 1
                    report["summary"]["pending"] -= 1
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(process_case, case, cases_by_id, client, graph, dataset): index
                    for index, case in enumerate(cases)
                    if _case_skip_reason(case) is None
                }
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    try:
                        result = future.result()
                    except Exception as error:  # noqa: BLE001 - keep the report complete
                        result = {
                            "id": cases[index]["id"],
                            "status": "failed",
                            "issues": [f"Case runner crashed: {error}"],
                        }
                    result["question"] = cases[index].get("question", "")
                    result["scenario"] = cases[index].get("scenario")
                    report["cases"][index] = result
                    report["summary"][result["status"]] += 1
                    for axis in ("automated", "semantic"):
                        if axis_status := result.get(f"{axis}_status"):
                            report["summary"][f"{axis}_{axis_status}"] += 1
                    if result.get("error_type") == "TurnTimeout":
                        report["summary"]["timed_out"] += 1
                    for rubric in result.get("semantic_rubrics") or []:
                        report["summary"][f"rubrics_{rubric['status']}"] += 1
                    report["summary"]["pending"] -= 1
                    save()
                    print(f"Completed case: {result['id']} -> {result['status']}")
        report["status"] = "completed"
        report["finished_at"] = datetime.now(UTC).isoformat()
    finally:
        if report["status"] != "completed":
            report["status"] = "interrupted"
        try:
            save()
        finally:
            if owns_dataset and dataset is not None:
                dataset.close()

    print("\n--- Evaluation Summary ---")
    print(json.dumps(report["summary"], indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--data-file",
        type=Path,
        help="Pinned golden CSV; downloaded when --download is given",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the pinned golden CSV if it is missing",
    )
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/eval-report.json")
    args = parser.parse_args()
    try:
        run_eval(
            args.questions,
            args.output,
            data_file=args.data_file,
            manifest_path=args.manifest,
            download=args.download,
            max_workers=args.workers,
        )
    except ValueError as error:
        parser.error(str(error))
