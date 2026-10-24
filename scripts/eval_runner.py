"""Evaluation Runner for the BI Agent.

Runs the golden question set through the real agent graph, against the pinned
golden dataset loaded into DuckDB, and writes a report with per-case status,
duration, tool calls and numerical checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
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


def _decimal(value: Any) -> Decimal | None:
    """Numeric value of a cell, excluding booleans and non-finite numbers."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal)):
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


def check_fact(
    fact: dict, results: Sequence[Any], *, column_aliases: dict[str, str] | None = None
) -> bool:
    """Check one reference fact against tool results only; prose is not evidence."""
    expected = fact["value"]
    comparison = fact["comparison"]

    if comparison == "approx":
        target = _decimal(expected)
        if target is None:
            return False
        tolerance = _tolerance(fact)
        return any(abs(number - target) <= tolerance for number in _structured_numbers(results))

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
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def evaluate_case(
    case: dict, result: dict, *, column_aliases: dict[str, str] | None = None
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

    issues = _terminal_issues(blocks, results, plan)
    if issues:
        return "failed", issues, details
    if called_forbidden or planned_forbidden:
        violations = sorted(set(called_forbidden + planned_forbidden))
        issues.append(f"Used or planned forbidden tools: {violations}")

    if outcome == "answer":
        tools_any = set(expect.get("tools_any") or []) | (
            set(expect.get("tools_planned") or []) & SUPPORTED_TOOLS
        )
        if tools_any and not set(tools_any).intersection(used_tools):
            issues.append(f"Did not use any expected tools {tools_any}. Used: {used_tools}")
        if not any(
            _field(block, "type") == "text" and _field(block, "markdown", "").strip()
            for block in blocks
        ):
            issues.append("The answer contains no text block")

        for fact in expect.get("facts") or []:
            result_ok = check_fact(fact, results, column_aliases=column_aliases)
            details["fact_checks"].append(
                {
                    "key": fact["key"],
                    "comparison": fact["comparison"],
                    "expected": fact["value"],
                    "result": result_ok,
                    "passed": result_ok,
                }
            )
            if not result_ok:
                issues.append(
                    f"Fact '{fact['key']}' (value: {fact['value']}) "
                    "is not confirmed by tool results"
                )
        # The answer is checked separately: every number it states must come from the tools.
        issues.extend(_answer_issues(case, text, results))

    elif outcome == "clarify":
        intent = _intent(result)
        if intent != expect.get("intent"):
            issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not any(
            _field(block, "type") == "clarification" and _field(block, "markdown")
            for block in blocks
        ):
            issues.append("Clarification outcome produced no clarification block")
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
                issues.append("Clarification provides fewer than two distinct options")
            details["unchecked_rubrics"].append("clarification.resolves_and_options_meaning")

    elif outcome == "reject":
        intent = _intent(result)
        if intent != expect.get("intent"):
            issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not text.strip():
            issues.append("Rejection produced no answer blocks")

    else:
        issues.append(f"Unsupported expected outcome '{outcome}'")

    return ("passed" if not issues else "failed"), issues, details


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

    def dispatch(self, tool: str, params: dict[str, Any]) -> ToolResult:
        with duckdb.connect(
            str(self.database_path),
            read_only=True,
            config={"enable_external_access": False},
        ) as connection:
            return execute_tool(
                tool, params, connection=connection, allowed_tables=self.allowed_tables
            )

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
            _, _, details = evaluate_case(dependency, partial, column_aliases=column_aliases)
            context_results.append(
                {
                    "id": dependency_id,
                    "status": "failed",
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
            dependency, dependency_result, column_aliases=column_aliases
        )
        context_results.append(
            {
                "id": dependency_id,
                "status": status,
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
        _, _, details = evaluate_case(case, partial, column_aliases=column_aliases)
        return {
            **details,
            "id": case_id,
            "status": "failed",
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
    status, issues, details = evaluate_case(case, result, column_aliases=column_aliases)
    return {
        **details,
        "id": case_id,
        "status": status,
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
            "semantic_rubrics": "not_run",
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
