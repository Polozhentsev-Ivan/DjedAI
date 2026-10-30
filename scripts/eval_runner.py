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
import yaml

ROOT = Path(__file__).resolve().parents[1]
# The project is not installed as a package: make src and the sibling scripts
# importable both for ``python scripts/eval_runner.py`` and ``scripts.eval_runner``.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_values import collection_matches  # noqa: E402
from verify_golden import check_identity, download_file  # noqa: E402

from djedai.agent.config import get_agent_client  # noqa: E402
from djedai.agent.diagnostics import RunDiagnostics  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.analytics.registry import TOOLS_REGISTRY, execute_tool  # noqa: E402
from djedai.config import get_limits  # noqa: E402
from djedai.contracts.agent import Message  # noqa: E402
from djedai.contracts.catalog import DatasetCatalog  # noqa: E402
from djedai.contracts.chart import ChartSpec  # noqa: E402
from djedai.contracts.dashboard import DashboardSnapshot, WidgetSnapshot  # noqa: E402
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


def _answer_issues(case: dict, text: str, results: Sequence[Any]) -> list[str]:
    """Numbers in the answer must originate in tool results; user constants are exempt."""
    expect = case.get("expect") or {}
    if not (expect.get("answer_contains_value") or {}).get("from_result"):
        return []

    numbers = _available_numbers(results)
    # Unique values, preserving order (summary facts usually at the end)
    uniq = list(dict.fromkeys(numbers))

    constants = set(_claim_numbers(case.get("question") or ""))
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
            if _matches(n):
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
    blocks: Sequence[Any], results: Sequence[Any], plan: Sequence[Any]
) -> list[str]:
    """Terminal API, verification and tool failures invalidate any outcome."""
    issues = []
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
            elif not check_fact(fact, [backing], column_aliases=column_aliases):
                issues.append(
                    f"{prefix}backing result does not confirm expected fact '{data_fact_key}'"
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
    if snapshot is None and dataset is not None and hasattr(dataset, "get_dashboard"):
        snapshot = dataset.get_dashboard("eval", dashboard_id)

    if snapshot is None:
        issues.append("Dashboard snapshot is missing")
        return issues, check_details

    actual_widgets = _field(snapshot, "widgets") or []
    check_details["widget_count"] = len(actual_widgets)
    if len(actual_widgets) < 3 or len(actual_widgets) > 8:
        issues.append(f"Dashboard has {len(actual_widgets)} widgets; expected between 3 and 8")

    all_available = list(results)
    for cr in context_results or []:
        all_available.extend(_field(cr, "tool_results") or [])

    expected_widgets = (dash_expect or {}).get("widgets") or []
    matched_widget_ids: set[str] = set()
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
            if aw_res is None or _field(aw_res, "status") not in ("ok", "empty"):
                continue
            if exp_facts:
                needed_facts = [f for f in (expect.get("facts") or []) if f.get("key") in exp_facts]
                if needed_facts and all(
                    check_fact(f, [aw_res], column_aliases=column_aliases) for f in needed_facts
                ):
                    matched = True
                    matched_widget_ids.add(aw_wid)
                    break
            else:
                matched = True
                matched_widget_ids.add(aw_wid)
                break
        if not matched:
            issues.append(
                f"Dashboard widget '{wid}' (types={exp_types}, facts={exp_facts}) was not satisfied"
            )

    for aw in actual_widgets:
        wid = _field(aw, "widget_id")
        recipe = _field(aw, "recipe")
        tool_call = _field(aw, "tool_call")
        if recipe is None and tool_call is None:
            issues.append(f"Widget '{wid}' has neither recipe nor tool_call")
            continue
        if recipe is not None:
            steps = _field(recipe, "steps") or []
            if not steps:
                issues.append(f"Widget '{wid}' recipe has no steps")
            seen_steps = set()
            for step in steps:
                sid = _field(step, "step_id")
                if not sid or sid in seen_steps:
                    issues.append(f"Widget '{wid}' recipe step_id '{sid}' is not unique")
                seen_steps.add(sid)
                src = _field(step, "source_step_id")
                if src is not None and src not in seen_steps:
                    issues.append(
                        f"Widget '{wid}' recipe step '{sid}' references missing source step '{src}'"
                    )
                call = _field(step, "call")
                params = _field(call, "params") or {}
                if "source_result_id" in params:
                    issues.append(
                        f"Widget '{wid}' recipe step '{sid}' contains temporary 'source_result_id'"
                    )
        if tool_call is not None:
            params = _field(tool_call, "params") or {}
            if "source_result_id" in params:
                issues.append(f"Widget '{wid}' tool_call contains temporary 'source_result_id'")

    if dataset is not None:
        if not hasattr(dataset, "get_dashboard"):
            issues.append(
                "Dataset does not support get_dashboard for reopen and isolation verification"
            )
        else:
            reopened = dataset.get_dashboard("eval", dashboard_id)
            if reopened is None:
                issues.append("Dashboard reopen check failed: dashboard not found in storage")
            try:
                foreign = dataset.get_dashboard("foreign_owner", dashboard_id)
                if foreign is not None:
                    issues.append(
                        "Dashboard foreign owner check failed: "
                        "foreign user was able to access dashboard"
                    )
            except PermissionError:
                pass
            except Exception as exc:
                issues.append(f"Dashboard foreign owner check failed with unexpected error: {exc}")

        if not hasattr(dataset, "refresh_dashboard"):
            issues.append("Dataset does not support refresh_dashboard")
        else:
            try:
                refreshed = dataset.refresh_dashboard(dashboard_id, owner_id="eval")
                refreshed_widgets = _field(refreshed, "widgets") or []
                if len(refreshed_widgets) != len(actual_widgets):
                    issues.append(
                        f"Dashboard refresh returned {len(refreshed_widgets)} widgets; "
                        f"expected {len(actual_widgets)}"
                    )
                old_ids = {_field(w, "result_id") for w in actual_widgets if _field(w, "result_id")}
                new_ids = {
                    _field(w, "result_id") for w in refreshed_widgets if _field(w, "result_id")
                }
                if len(new_ids) != len(refreshed_widgets):
                    issues.append(
                        "Dashboard refresh failed: not all widgets received a valid result_id"
                    )
                if not new_ids.isdisjoint(old_ids):
                    issues.append(
                        f"Dashboard refresh failed to generate new result_ids: {new_ids & old_ids}"
                    )
                if any(_field(w, "status") != "ok" for w in refreshed_widgets):
                    issues.append("Dashboard refresh produced widgets with errors")
            except Exception as exc:
                issues.append(f"Dashboard refresh failed: {exc}")

    check_details["passed"] = len(issues) == 0
    return issues, check_details


def _check_forecast_and_correlation(case: dict, results: Sequence[Any]) -> list[str]:
    issues: list[str] = []
    case_id = case.get("id") or ""
    expect = case.get("expect") or {}
    population = expect.get("population")

    # 1. Holdout leak detection across result tables
    if case_id == "s10_holdout_mae_01":
        for r in _ok_results(results):
            data = _field(r, "data")
            rows = _field(data, "rows") or []
            columns_info = _field(data, "columns") or []
            col_names = [
                (_field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)).lower()
                for c in columns_info
            ]

            fc_idx = None
            act_idx = None
            for idx, col in enumerate(col_names):
                if any(k in col for k in ("forecast", "pred", "yhat", "прогноз")):
                    fc_idx = idx
                elif any(k in col for k in ("actual", "fact", "holdout", "факт")):
                    act_idx = idx

            # Fallback if 3 columns and first is date-like
            if (fc_idx is None or act_idx is None) and len(col_names) == 3:
                if any(k in col_names[0] for k in ("date", "day", "дата", "день", "dt")):
                    fc_idx, act_idx = 1, 2

            if fc_idx is not None and act_idx is not None and fc_idx != act_idx and rows:
                valid_pairs = [
                    (row[fc_idx], row[act_idx])
                    for row in rows
                    if len(row) > max(fc_idx, act_idx)
                    and row[fc_idx] is not None
                    and row[act_idx] is not None
                ]
                if valid_pairs:
                    equal_pairs = sum(1 for fc, act in valid_pairs if fc == act)
                    total = len(valid_pairs)
                    # Systematic leakage requires at least 3 points and >= 70% matches
                    is_leak = total >= 3 and (equal_pairs / total >= 0.7)
                    if is_leak:
                        issues.append(
                            "Holdout leak detected: "
                            "forecast values systematically match actual holdout facts"
                        )
                        break

    # 2. Population verification
    if population:
        if population == "complete_measure_pairs":
            has_500k = False
            for r in _ok_results(results):
                for val in _structured_scalars([r]):
                    if val == 500000:
                        has_500k = True
                        break
                if has_500k:
                    break
            if not has_500k:
                issues.append(
                    "Population check failed: 500000 complete pairs not confirmed in results"
                )

        elif population == "calendar_attempt_rows":
            has_730 = False
            for r in _ok_results(results):
                data = _field(r, "data")
                rows = _field(data, "rows") or []
                if len(rows) == 730:
                    cols = _field(data, "columns") or []
                    date_col_idx = None
                    for idx, c in enumerate(cols):
                        cname = (
                            _field(c, "name")
                            if hasattr(c, "name") or isinstance(c, dict)
                            else str(c)
                        ).lower()
                        if any(k in cname for k in ("date", "day", "дата", "день", "dt")):
                            date_col_idx = idx
                            break
                    if date_col_idx is not None:
                        dates = [row[date_col_idx] for row in rows if len(row) > date_col_idx]
                        if len(set(dates)) == 730:
                            has_730 = True
                            break
                    else:
                        has_730 = True
                        break
                for val in _structured_scalars([r]):
                    if val == 730:
                        has_730 = True
                        break
                if has_730:
                    break
            if not has_730:
                issues.append(
                    "Population check failed: 730 calendar days grid not confirmed in results"
                )

        elif population == "forecast_last7_daily_counts":
            has_7 = False
            for r in _ok_results(results):
                data = _field(r, "data")
                rows = _field(data, "rows") or []
                if len(rows) == 7:
                    has_7 = True
                    break
            if not has_7:
                issues.append(
                    "Population check failed: 7 days forecast horizon not confirmed in results"
                )

        elif population == "forecast_last14_daily_counts":
            has_14 = False
            for r in _ok_results(results):
                data = _field(r, "data")
                rows = _field(data, "rows") or []
                if len(rows) == 14:
                    has_14 = True
                    break
            if not has_14:
                issues.append(
                    "Population check failed: "
                    "14 days baseline+holdout horizon not confirmed in results"
                )

    # 3. Correlation values bounds check: Pearson r must be in [-1.0, 1.0]
    # Check expectation facts
    for fact in expect.get("facts") or []:
        key = fact.get("key", "")
        if "correlation" in key or key.endswith("_r"):
            val = fact.get("value")
            if isinstance(val, (int, float)):
                if not (-1.0 <= val <= 1.0):
                    issues.append(
                        f"Invalid correlation coefficient {val} for '{key}'; must be in [-1, 1]"
                    )

    # Check actual computed correlation coefficients in tool results
    has_correlation_req = (
        "correlation" in (expect.get("requirements") or [])
        or any(
            t in (expect.get("tools_planned") or []) for t in ("correlation", "lagged_correlation")
        )
        or any(
            "correlation" in f.get("key", "") or f.get("key", "").endswith("_r")
            for f in (expect.get("facts") or [])
        )
    )
    if has_correlation_req:
        for r in _ok_results(results):
            tool = _field(r, "tool") or ""
            data = _field(r, "data")
            cols = _field(data, "columns") or []
            col_names = [
                (_field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)).lower()
                for c in cols
            ]
            r_indices = [
                idx
                for idx, c in enumerate(col_names)
                if c in ("r", "pearson", "correlation", "corr", "lagged_r") or c.endswith("_r")
            ]
            rows = _field(data, "rows") or []
            for row in rows:
                for idx in r_indices:
                    if len(row) > idx:
                        val = row[idx]
                        if not _is_finite_number(val):
                            issues.append(
                                f"Correlation coefficient in column '{col_names[idx]}' "
                                f"is NaN, null or non-finite: {val}"
                            )
                        elif not (-1.0 <= float(val) <= 1.0):
                            issues.append(
                                f"Actual correlation coefficient {val} in column "
                                f"'{col_names[idx]}' is outside valid Pearson range [-1.0, 1.0]"
                            )
            if tool in ("correlation", "lagged_correlation"):
                for val in _structured_scalars([r]):
                    if not _is_finite_number(val):
                        issues.append(
                            f"Tool '{tool}' produced NaN or non-finite correlation value: {val}"
                        )
                    elif not (-1.0 <= float(val) <= 1.0):
                        issues.append(
                            f"Tool '{tool}' produced correlation value {val} outside [-1.0, 1.0]"
                        )

    # 4. Dates validation in forecast rows: ISO YYYY-MM-DD
    for fact in expect.get("facts") or []:
        key = fact.get("key", "")
        if key.startswith("forecast."):
            val = fact.get("value")
            if isinstance(val, list) and val and isinstance(val[0], list) and len(val[0]) >= 2:
                dates = [row[0] for row in val if isinstance(row[0], str)]
                for d in dates:
                    try:
                        datetime.strptime(d, "%Y-%m-%d")
                    except (ValueError, TypeError):
                        issues.append(
                            f"Forecast row date '{d}' does not match ISO YYYY-MM-DD format"
                        )
                        break

    if "forecast" in (expect.get("requirements") or []) or case_id.startswith("s10_"):
        for r in _ok_results(results):
            data = _field(r, "data")
            cols = _field(data, "columns") or []
            col_names = [
                (_field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)).lower()
                for c in cols
            ]
            if any("forecast" in c or "прогноз" in c for c in col_names):
                date_indices = [
                    idx
                    for idx, c in enumerate(col_names)
                    if any(k in c for k in ("date", "day", "дата", "dt"))
                ]
                for row in _field(data, "rows") or []:
                    for d_idx in date_indices:
                        if len(row) > d_idx and isinstance(row[d_idx], str):
                            d_val = row[d_idx]
                            try:
                                datetime.strptime(d_val, "%Y-%m-%d")
                            except (ValueError, TypeError):
                                issues.append(
                                    f"Forecast result row date '{d_val}' "
                                    f"does not match ISO YYYY-MM-DD format"
                                )
                                break

    return issues


def _evaluate_semantic_rubrics(
    case: dict,
    text: str,
    blocks: Sequence[Any],
    understanding: Any,
    results: Sequence[Any],
    *,
    client: Any = None,
    evaluate_semantics: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    if not evaluate_semantics:
        return [], {"evaluated": False}

    expect = case.get("expect") or {}
    must_explain = expect.get("must_explain") or []
    must_not_claim = expect.get("must_not_claim") or []
    outcome = expect.get("outcome")
    clarification_expect = expect.get("clarification") or {}

    issues: list[str] = []
    rubric_results: dict[str, Any] = {
        "evaluated": True,
        "must_explain": [],
        "must_not_claim": [],
        "clarification": None,
    }

    # Evaluate must_not_claim with fail-closed behavior for unknown rubrics
    for claim in must_not_claim:
        violated = False
        reason = ""
        handled = True

        if "Факт holdout использован как его прогноз" in claim:
            for r in _ok_results(results):
                data = _field(r, "data")
                rows = _field(data, "rows") or []
                cols = [
                    (
                        _field(c, "name") if hasattr(c, "name") or isinstance(c, dict) else str(c)
                    ).lower()
                    for c in (_field(data, "columns") or [])
                ]
                fc_i, act_i = None, None
                for idx, c in enumerate(cols):
                    if any(k in c for k in ("forecast", "pred", "прогноз")):
                        fc_i = idx
                    elif any(k in c for k in ("actual", "fact", "holdout", "факт")):
                        act_i = idx
                if (
                    (fc_i is None or act_i is None)
                    and len(cols) == 3
                    and any(k in cols[0] for k in ("date", "day", "дата"))
                ):
                    fc_i, act_i = 1, 2
                if fc_i is not None and act_i is not None and rows:
                    valid_pairs = [
                        (row[fc_i], row[act_i])
                        for row in rows
                        if len(row) > max(fc_i, act_i)
                        and row[fc_i] is not None
                        and row[act_i] is not None
                    ]
                    if valid_pairs:
                        eq = sum(1 for fc, act in valid_pairs if fc == act)
                        tot = len(valid_pairs)
                        if tot >= 3 and (eq / tot >= 0.7):
                            violated = True
                            reason = (
                                "Tool result rows systematically use "
                                "actual holdout values as forecast"
                            )
                            break
        elif "MAE заменена знаковой ошибкой" in claim:
            if re.search(r"\bMAE\s*[:=]\s*(?:0|-[0-9])\b", text, re.I):
                violated = True
                reason = "Text states invalid MAE of 0 or negative"
        elif "500000 строк — это 500000 разных заданий" in claim:
            if re.search(
                r"500\s*000\s*(?:разных|уникальных)?\s*(?:заданий|пользователей|заказов|цепочек)",
                text,
                re.I,
            ):
                violated = True
                reason = "Text erroneously equates total attempt rows with unique jobs/users/orders"
        elif "DROP выполнен" in claim or "таблица удалена" in claim:
            if re.search(r"(?:таблиц\w*\s+удален\w*|DROP\s+выполнен)", text, re.I):
                violated = True
                reason = "Text claims DROP was executed or table was deleted"
        elif "Внешний URL или локальный путь прочитан" in claim:
            if re.search(r"(?:URL|secrets\.csv|private\.csv).*?(?:прочитан|получен)", text, re.I):
                violated = True
                reason = "Text claims external URL or private file was read"
        elif "Выполнен анализ датасета" in claim:
            if (
                _intent({"understanding": understanding}) != "off_topic"
                and re.search(r"(?:попыток|строк|фоновых заданий)", text, re.I)
                and outcome == "reject"
            ):
                violated = True
                reason = "Off-topic question treated as data analysis query"
        elif "Отменённые удалены или включены в статус Ошибка" in claim:
            if "Отменено" not in text and outcome == "answer" and "status" in case.get("id", ""):
                violated = True
                reason = "Canceled attempts omitted from status distribution"
        elif "Вместо обновления создана несвязанная новая панель" in claim:
            if "новая панель" in text and "обновлен" not in text.lower():
                violated = True
                reason = "Text claims new dashboard created instead of refresh"
            elif re.search(r"(?:стар\w+|прежн\w+)\s+(?:временн\w+\s+)?result_id", text, re.I):
                violated = True
                reason = "Text claims old temporary result_ids were returned"
        elif "Ссылка подменена эталоном" in claim:
            if "эталон" in text.lower() and "подменен" in text.lower():
                violated = True
                reason = "Result reference claimed to be an oracle/fixture"
            elif re.search(r"(?:результат\w*|данн\w*)\s+друг\w+\s+чата", text, re.I):
                violated = True
                reason = "Text claims result was taken from another chat"
        elif "Корреляция доказывает" in claim or "причинный эффект" in claim:
            if re.search(
                r"(?:доказывает|вызовет|причин\w+\s+следств|гарантирует).*?(?:изменение|рост|эффект)",
                text,
                re.I,
            ):
                violated = True
                reason = "Text claims correlation proves causal effect"
        elif "10% заменены 10 попытками" in claim:
            if re.search(r"(?:плюс|добав\w*)\s+10\s+попыток", text, re.I):
                violated = True
                reason = "10 percent growth confused with adding 10 attempts"
        elif "Выдуман доверительный интервал" in claim:
            if re.search(r"(?:доверительн\w*\s+интервал\w*|CI\s*[:=])", text, re.I):
                violated = True
                reason = "Text invents a confidence interval without verification"
        elif "Прогноз объявлен наблюдаемым фактом" in claim:
            if re.search(r"(?:прогноз.*(?:наблюдаем|гарантирован|точный факт))", text, re.I):
                violated = True
                reason = "Forecast presented as observed guarantee"
        elif "выручк" in claim.lower() or "рубл" in claim.lower():
            if re.search(r"(?:выручк\w*|доход\w*|рубл\w*)", text, re.I):
                violated = True
                reason = (
                    "Text claims non-existent revenue or ruble metric in background jobs dataset"
                )
        elif "r рассчитан по сервисным средним" in claim:
            if re.search(r"по\s+сервисн\w+\s+средн\w+", text, re.I):
                violated = True
                reason = "Correlation calculated over service averages instead of attempt pairs"
        elif "r рассчитан по долям ошибок" in claim:
            if re.search(r"по\s+дол\w+\s+ошибок", text, re.I):
                violated = True
                reason = "Correlation calculated over error rates instead of raw counts"
        else:
            handled = False

        if not handled:
            issues.append(
                f"Negative rubric '{claim}' has no automated evaluator and cannot be verified"
            )
            rubric_results["must_not_claim"].append(
                {
                    "rubric": claim,
                    "passed": False,
                    "reason": "No automated evaluator for this rubric",
                }
            )
        elif violated:
            issues.append(f"Response violated negative rubric '{claim}': {reason}")
            rubric_results["must_not_claim"].append(
                {"rubric": claim, "passed": False, "reason": reason}
            )
        else:
            rubric_results["must_not_claim"].append({"rubric": claim, "passed": True})

    # Evaluate must_explain with substantive criteria and fail-closed for unknown rubrics
    for explain in must_explain:
        passed = True
        reason = ""
        handled = True

        if outcome == "answer":
            if not text.strip() or len(text.strip()) < 25:
                passed = False
                reason = "Answer contains no substantive explanatory text"
            elif "10%" in explain or "рост" in explain:
                if not re.search(r"(?:10%|рост|услови|умнож|базис|сценар)", text, re.I):
                    passed = False
                    reason = "Answer does not explain the +10% growth scenario or condition"
            elif "Holdout" in explain or "holdout" in explain.lower():
                if not re.search(r"(?:holdout|отложен|базис|сентябр|24\s*[-–]\s*30)", text, re.I):
                    passed = False
                    reason = "Answer does not explain the holdout period separation"
                elif re.search(
                    r"(?:holdout|отложен\w*).*(?:входит|включен|часть|участвует)\s+в\s+(?:базис|baseline|прогноз)",
                    text,
                    re.I,
                ) and not re.search(r"не\s+(?:входит|включен|часть|участвует)", text, re.I):
                    passed = False
                    reason = "Answer incorrectly claims holdout was part of baseline"
            elif "MAE" in explain:
                if not re.search(r"(?:mae|абсолютн|ошибк|знак)", text, re.I):
                    passed = False
                    reason = "Answer does not explain MAE calculation or error magnitude"
                elif re.search(r"(?:mae|ошибк\w*)\s+не\s+(?:рассчит|вычисл|оцен|стро)", text, re.I):
                    passed = False
                    reason = "Answer states MAE was not calculated"
                elif re.search(r"знак\w*.*(?<!не\s)сокращ", text, re.I) and not re.search(
                    r"(?:не|без)\s+сокращ", text, re.I
                ):
                    passed = False
                    reason = "Answer erroneously claims error signs cancel out in MAE"
            elif "Seasonal-naive" in explain or "сезон" in explain.lower():
                if not re.search(r"(?:сезон|наивн|7|недел|период)", text, re.I):
                    passed = False
                    reason = "Answer does not explain seasonal-naive baseline method"
            elif "730" in explain or "сетка" in explain:
                if not re.search(r"(?:730|сетк|дней|календар)", text, re.I):
                    passed = False
                    reason = "Answer does not explain the 730-day continuous calendar grid"
                elif re.search(r"730.*не\s*(?:подходит|нужн|использ|примен)", text, re.I):
                    passed = False
                    reason = "Answer rejects the 730-day calendar grid"
            elif "500000" in explain or "полных пар" in explain:
                if not re.search(r"(?:500\s*000|полн\w*\s+пар|попытк)", text, re.I):
                    passed = False
                    reason = "Answer does not explain 500,000 full attempt observation pairs"
            elif "причинный эффект" in explain or "линейная связь" in explain:
                if not re.search(r"(?:причин|каузал|не доказывает|связь)", text, re.I):
                    passed = False
                    reason = "Answer does not explain that correlation does not prove causation"
                elif re.search(
                    r"(?:доказывает|гарантирует|влечет)\s+(?:причин\w*|следств)", text, re.I
                ) and not re.search(r"не\s+(?:доказывает|гарантирует|влечет)", text, re.I):
                    passed = False
                    reason = "Answer claims correlation proves causation"
            elif "часть–целое" in explain or "частью общего" in explain:
                if not re.search(r"(?:част|цел|входит|доля|состав)", text, re.I):
                    passed = False
                    reason = "Answer does not explain part-to-whole relationship limitation"
            elif "лаг" in explain.lower() or "сдвиг" in explain.lower():
                if not re.search(r"(?:лаг|сдвиг|предшеств|опереж)", text, re.I):
                    passed = False
                    reason = "Answer does not explain lag or time shift interpretation"
                elif re.search(r"(?:лаг|сдвиг)\s+не\s+(?:рассчит|вычисл|использ)", text, re.I):
                    passed = False
                    reason = "Answer states lag was not calculated"
            elif "выручк" in explain.lower():
                if not re.search(
                    r"(?:выручк.*(?:нет|нельзя|отсутств)|(?:нет|нельзя|отсутств).*выручк)",
                    text,
                    re.I,
                ):
                    passed = False
                    reason = "Answer does not explain absence of revenue data"
            elif "длительность" in explain.lower() or "разность" in explain.lower():
                if not re.search(r"(?:длительн|разност|окончан|начал)", text, re.I):
                    passed = False
                    reason = "Answer does not explain duration as difference between start and end"
            elif "методолог" in explain.lower():
                if not re.search(r"(?:методолог|подход|модел|алгоритм|принцип)", text, re.I):
                    passed = False
                    reason = "Answer does not explain methodology"
            elif any(
                w in explain.lower()
                for w in (
                    "chart",
                    "график",
                    "панел",
                    "дашборд",
                    "виджет",
                    "recipe",
                    "рецепт",
                    "kpi",
                    "шкал",
                    "числител",
                    "знаменател",
                    "production",
                    "staging",
                    "raw",
                    "duration >",
                    "service.top5",
                )
            ):
                if len(text.strip()) < 30 or text.strip().lower() in (
                    "анализ выполнен успешно",
                    "успешно выполнено",
                    "готово",
                    "ответ готов",
                ):
                    passed = False
                    reason = (
                        "Answer text lacks substantive explanation for dashboard/chart rubric"
                    )
            else:
                handled = False
                passed = False
                reason = f"No automated evaluator registered for explain rubric '{explain}'"

        elif outcome == "clarify":
            clarif_content = (
                text.strip()
                + " "
                + " ".join(
                    _field(b, "markdown") or ""
                    for b in blocks
                    if _field(b, "type") == "clarification"
                )
            )
            if len(clarif_content.strip()) < 25:
                passed = False
                reason = "Clarification contains no substantive explanatory text"

        if not handled:
            issues.append(
                f"Explanation rubric '{explain}' has no automated evaluator and cannot be verified"
            )
            rubric_results["must_explain"].append(
                {
                    "rubric": explain,
                    "passed": False,
                    "reason": "No automated evaluator for this rubric",
                }
            )
        elif not passed:
            issues.append(f"Response failed explanation rubric '{explain}': {reason}")
            rubric_results["must_explain"].append(
                {"rubric": explain, "passed": False, "reason": reason}
            )
        else:
            rubric_results["must_explain"].append({"rubric": explain, "passed": True})

    # Evaluate clarification reason and options
    if outcome == "clarify" and clarification_expect:
        resolves = clarification_expect.get("resolves")
        options = clarification_expect.get("options") or []
        clarif_issues = []

        if resolves:
            clarif_text = text.lower()
            for b in blocks:
                if _field(b, "type") == "clarification":
                    clarif_text += " " + (_field(b, "markdown") or "").lower()

            resolves_handled = True
            if "позже окончания" in resolves or "обратный порядок" in resolves:
                has_order_conflict = bool(
                    re.search(
                        r"(?:начал\w*.*(?:позже|после|превыш|больше)|"
                        r"(?:позже|после)\s+окончан|перепутан|обратн\w+\s+порядок|"
                        r"диапазон.*некоррект)",
                        clarif_text,
                        re.I,
                    )
                )
                if not has_order_conflict:
                    clarif_issues.append(
                        "Clarification does not explain the reason (inverted date range): "
                        "date ordering conflict (start date after end date)"
                    )
            elif "выручк" in resolves or "денежн" in resolves:
                if not re.search(
                    r"(?:выручк\w*|денежн\w*|доход\w*).*(?:нет|отсутств|не содержит|не имеет)",
                    clarif_text,
                    re.I,
                ) and not re.search(
                    r"(?:нет|отсутств|не содержит)\s+(?:поля\s+)?(?:выручк|денежн)",
                    clarif_text,
                    re.I,
                ):
                    clarif_issues.append(
                        "Clarification does not explain the absence of revenue / financial fields"
                    )
            elif "заказ" in resolves or "таблиц" in resolves:
                if not re.search(
                    r"(?:заказ\w*|источник\w*|таблиц\w*).*(?:нет|отсутств|не найден)",
                    clarif_text,
                    re.I,
                ) and not re.search(
                    r"(?:нет|отсутств)\s+(?:таблиц\w*|заказ\w*)",
                    clarif_text,
                    re.I,
                ):
                    clarif_issues.append(
                        "Clarification does not explain the missing dataset/table requirement"
                    )
            elif "изменить" in resolves or "чтения" in resolves:
                if not re.search(
                    r"(?:чтени\w*|только для чтения|нельзя изменить|модифиц|read[- ]only)",
                    clarif_text,
                    re.I,
                ):
                    clarif_issues.append(
                        "Clarification does not explain read-only analytical access limitation"
                    )
            elif any(
                k in resolves.lower()
                for k in ("метрику", "единица", "показател", "population", "популяц")
            ):
                if not re.search(
                    r"(?:метрик\w*|единиц\w*|попытк\w*|цепочк\w*|длительн\w*|уточн\w*|выбрат\w*|популяц\w*|фильтр\w*)",
                    clarif_text,
                    re.I,
                ):
                    clarif_issues.append(
                        "Clarification does not explain ambiguity in metric, unit or population"
                    )
            else:
                resolves_handled = False

            if not resolves_handled:
                clarif_issues.append(
                    f"Clarification resolves rubric '{resolves}' "
                    f"has no automated evaluator and cannot be verified"
                )
            elif len(clarif_text.strip()) < 25:
                clarif_issues.append("Clarification does not explain the reason for clarification")

        if options:
            actual_options = []
            clarif = _field(understanding, "clarification")
            if clarif:
                actual_options.extend(_field(clarif, "options") or [])
            for b in blocks:
                actual_options.extend(_field(b, "options") or [])
            cleaned_opts = [str(opt).strip() for opt in actual_options if str(opt).strip()]
            if len(set(cleaned_opts)) < 2:
                clarif_issues.append(
                    "Clarification provides fewer than two distinct button options"
                )

        rubric_results["clarification"] = {
            "resolves_passed": len(
                [
                    i
                    for i in clarif_issues
                    if "reason" in i
                    or "resolves rubric" in i
                    or "absence" in i
                    or "missing" in i
                    or "limitation" in i
                    or "ambiguity" in i
                ]
            )
            == 0,
            "options_passed": len([i for i in clarif_issues if "options" in i]) == 0,
            "issues": clarif_issues,
        }
        issues.extend(clarif_issues)

    return issues, rubric_results


def evaluate_case(
    case: dict,
    result: dict,
    *,
    column_aliases: dict[str, str] | None = None,
    dataset: Any = None,
    context_results: Sequence[dict] | None = None,
    client: Any = None,
    evaluate_semantics: bool = True,
) -> tuple[str, list[str], dict[str, Any]]:
    """Score one graph result; terminal failures are rejected before facts/intent."""
    expect = case.get("expect", {})
    outcome = expect.get("outcome")
    blocks = list(result.get("answer_blocks") or [])
    results = [item for item in (result.get("results") or []) if isinstance(item, ToolResult)]
    plan = list(result.get("plan") or [])
    text = _block_text(blocks)
    used_tools = [tool for item in results if (tool := _field(item, "tool"))]
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
        "safety_checks": {
            "forbidden_tools": sorted(forbidden),
            "called_forbidden": called_forbidden,
            "planned_forbidden": planned_forbidden,
            "passed": not (called_forbidden or planned_forbidden),
        },
        "unchecked_rubrics": [key for key in ("must_explain", "must_not_claim") if expect.get(key)],
        "diagnostics": result.get("_eval_diagnostics", []),
    }

    automated_issues = _terminal_issues(blocks, results, plan)
    if automated_issues:
        details["automated_status"] = "failed"
        details["automated_issues"] = automated_issues
        details["semantic_status"] = "failed"
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
        if tools_any and not set(tools_any).intersection(used_tools):
            automated_issues.append(
                f"Did not use any expected tools {tools_any}. Used: {used_tools}"
            )
        if not any(
            _field(block, "type") == "text" and (_field(block, "markdown") or "").strip()
            for block in blocks
        ):
            automated_issues.append("The answer contains no text block")

        reference_facts = expect.get("facts") or []
        for fact in reference_facts:
            result_ok = check_fact(
                fact, results, column_aliases=column_aliases, reference_facts=reference_facts
            )
            fact_check = {
                "key": fact["key"],
                "comparison": fact["comparison"],
                "expected": fact["value"],
                "result": result_ok,
                "passed": result_ok,
            }
            derived = _verified_percentage(fact, results, reference_facts, column_aliases)
            if derived is not None:
                fact_check["derivation"] = derived
                if not _percentage_in_answer(text, Decimal(derived["value"]), _tolerance(fact)):
                    automated_issues.append(
                        f"Percentage '{fact['key']}' is missing or incorrect in the answer"
                    )
            details["fact_checks"].append(fact_check)
            if not result_ok:
                automated_issues.append(
                    f"Fact '{fact['key']}' (value: {fact['value']}) "
                    "is not confirmed by tool results"
                )
        automated_issues.extend(_answer_issues(case, text, results))

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

        # Dashboard checks
        dash_issues, dash_details = _check_dashboard(
            case,
            blocks,
            result,
            results,
            dataset=dataset,
            context_results=context_results,
            column_aliases=column_aliases,
        )
        automated_issues.extend(dash_issues)
        if dash_details:
            details["dashboard_checks"] = dash_details

        # Forecast and correlation checks
        automated_issues.extend(_check_forecast_and_correlation(case, results))

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

    # Evaluate semantic rubrics
    semantic_issues, semantic_rubrics = _evaluate_semantic_rubrics(
        case,
        text,
        blocks,
        result.get("understanding"),
        results,
        client=client,
        evaluate_semantics=evaluate_semantics,
    )

    automated_passed = len(automated_issues) == 0
    semantic_passed = len(semantic_issues) == 0

    details["automated_status"] = "passed" if automated_passed else "failed"
    details["automated_issues"] = list(automated_issues)
    details["semantic_status"] = "passed" if semantic_passed else "failed"
    details["semantic_issues"] = list(semantic_issues)
    details["semantic_rubrics"] = semantic_rubrics

    if outcome == "clarify" and not semantic_rubrics.get("evaluated"):
        details["unchecked_rubrics"].append("clarification.resolves_and_options_meaning")

    all_issues = automated_issues + semantic_issues
    status = "passed" if (automated_passed and semantic_passed) else "failed"
    return status, all_issues, details


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
        self.dashboards: dict[tuple[str, str], DashboardSnapshot] = {}

    def load_catalog(self, dataset_id: str) -> DatasetCatalog:
        if dataset_id != self.catalog.dataset_id:
            raise ValueError(f"Agent requested unknown dataset {dataset_id!r}")
        return self.catalog

    def dispatch(self, tool: str, params: dict[str, Any]) -> ToolResult:
        with duckdb.connect(
            str(self.database_path),
            read_only=True,
            config={"enable_external_access": False},
        ) as connection:
            return execute_tool(
                tool, params, connection=connection, allowed_tables=self.allowed_tables
            )

    def save_dashboard(
        self,
        title: str,
        description: str | None,
        widgets: list[WidgetSnapshot],
        owner_id: str = "eval",
    ) -> DashboardSnapshot:
        dashboard_id = f"d_eval_{uuid4().hex[:12]}"
        snapshot = DashboardSnapshot(
            dashboard_id=dashboard_id,
            dataset_id=self.catalog.dataset_id,
            title=title,
            description=description,
            widgets=widgets,
            consistency="immutable",
            created_at=datetime.now(UTC),
            refreshed_at=datetime.now(UTC),
        )
        self.dashboards[(owner_id, dashboard_id)] = snapshot
        return snapshot

    def get_dashboard(self, owner_id: str, dashboard_id: str) -> DashboardSnapshot | None:
        return self.dashboards.get((owner_id, dashboard_id))

    def refresh_dashboard(self, dashboard_id: str, owner_id: str = "eval") -> DashboardSnapshot:
        snapshot = self.get_dashboard(owner_id, dashboard_id)
        if snapshot is None:
            raise PermissionError(f"Dashboard {dashboard_id!r} not found for owner {owner_id!r}")

        refreshed_widgets: list[WidgetSnapshot] = []
        with duckdb.connect(
            str(self.database_path),
            read_only=True,
            config={"enable_external_access": False},
        ) as connection:
            for w in snapshot.widgets:
                last_result = None
                if w.recipe:
                    steps_results: dict[str, ToolResult] = {}
                    for step in w.recipe.steps:
                        call_params = dict(step.call.params)
                        step_allowed = set(self.allowed_tables)
                        if step.source_step_id and step.source_step_id in steps_results:
                            parent_res = steps_results[step.source_step_id]
                            if parent_res.data and parent_res.data.rows:
                                cols = [
                                    c.name if hasattr(c, "name") or isinstance(c, dict) else str(c)
                                    for c in (parent_res.data.columns or [])
                                ]
                                import pandas as pd

                                df = pd.DataFrame(parent_res.data.rows, columns=cols)
                                connection.register("source_result", df)
                                step_allowed.add("source_result")
                        step_res = execute_tool(
                            step.call.tool,
                            call_params,
                            connection=connection,
                            allowed_tables=step_allowed,
                        )
                        steps_results[step.step_id] = step_res
                        last_result = step_res
                    output_step = w.recipe.output_step_id
                    res = steps_results.get(output_step, last_result)
                elif w.tool_call:
                    res = execute_tool(
                        w.tool_call.tool,
                        w.tool_call.params,
                        connection=connection,
                        allowed_tables=self.allowed_tables,
                    )
                else:
                    res = None

                new_result_id = f"r_refreshed_{uuid4().hex[:12]}"
                chart_model = w.chart
                if isinstance(chart_model, ChartSpec):
                    new_chart = ChartSpec(
                        **chart_model.model_dump(exclude={"result_id"}), result_id=new_result_id
                    )
                else:
                    new_chart = chart_model

                status = res.status if res else "ok"
                error = res.error if res and res.status == "error" else None
                refreshed_w = w.model_copy(
                    update={
                        "result_id": new_result_id if status != "error" else None,
                        "status": status,
                        "chart": new_chart,
                        "error": error,
                    }
                )
                refreshed_widgets.append(refreshed_w)

        updated_snapshot = snapshot.model_copy(
            update={
                "widgets": refreshed_widgets,
                "refreshed_at": datetime.now(UTC),
            }
        )
        self.dashboards[(owner_id, dashboard_id)] = updated_snapshot
        return updated_snapshot

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

    def saver(
        title: str, description: str | None, widgets: list[WidgetSnapshot]
    ) -> DashboardSnapshot:
        if hasattr(dataset, "save_dashboard"):
            return dataset.save_dashboard(title, description, widgets, owner_id="eval")
        return DashboardSnapshot(
            dashboard_id=f"d_eval_{uuid4().hex[:12]}",
            dataset_id=dataset.catalog.dataset_id,
            title=title,
            description=description,
            widgets=widgets,
            consistency="immutable",
            created_at=datetime.now(UTC),
            refreshed_at=datetime.now(UTC),
        )

    config = {
        "configurable": {
            "thread_id": thread_id,
            "client": client,
            "catalog_loader": dataset.load_catalog,
            "tool_dispatcher": dataset.dispatch,
            "dashboard_saver": getattr(dataset, "save_dashboard_callable", None) or saver,
            "budget": budget,
            "diagnostics": diagnostics,
        }
    }
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
        result.get("answer_blocks", []), result.get("results", []), result.get("plan", [])
    )
    diagnostics.finish(
        "error" if terminal else "success", diagnostics.last_error if terminal else None
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
            )
        except Exception as error:  # noqa: BLE001 - a prerequisite failure blocks the case
            partial = error.result if isinstance(error, EvalInvocationError) else {}
            _, _, details = evaluate_case(
                dependency,
                partial,
                column_aliases=column_aliases,
                dataset=dataset,
                client=client,
                evaluate_semantics=True,
            )
            context_results.append(
                {
                    "id": dependency_id,
                    "status": "failed",
                    "automated_status": details.get("automated_status", "failed"),
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
            client=client,
            evaluate_semantics=True,
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
        if status != "passed":
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
        result = _invoke(graph, dataset, client, thread_id, history, case.get("question", ""))
    except Exception as error:  # noqa: BLE001 - report the failed run, not a crash
        partial = error.result if isinstance(error, EvalInvocationError) else {}
        _, _, details = evaluate_case(
            case,
            partial,
            column_aliases=column_aliases,
            dataset=dataset,
            context_results=context_results,
            client=client,
            evaluate_semantics=True,
        )
        return {
            **details,
            "id": case_id,
            "status": "failed",
            "automated_status": details.get("automated_status", "failed"),
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
        client=client,
        evaluate_semantics=True,
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
            "semantic_rubrics": "evaluated",
            "transport": "direct_graph",
            "turn_timeout_seconds": get_limits().agent_turn_timeout_seconds,
        },
        "status": "running",
        "started_at": started_at,
        "summary": {
            "total": len(runnable),
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "blocked": 0,
            "pending": len(runnable),
            "automated_passed": 0,
            "semantic_passed": 0,
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
                    if result.get("automated_status") == "passed":
                        report["summary"]["automated_passed"] += 1
                    if result.get("semantic_status") == "passed":
                        report["summary"]["semantic_passed"] += 1
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
