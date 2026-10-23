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
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
# The project is not installed as a package: make src and the sibling scripts
# importable both for ``python scripts/eval_runner.py`` and ``scripts.eval_runner``.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_golden import check_identity, download_file  # noqa: E402

from djedai.agent.config import get_agent_client  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.analytics.registry import execute_tool  # noqa: E402
from djedai.contracts.agent import Message  # noqa: E402
from djedai.contracts.catalog import DatasetCatalog  # noqa: E402
from djedai.contracts.tool_result import ToolResult  # noqa: E402
from djedai.data.service import CsvSource, ingest_csv_dataset  # noqa: E402

DEFAULT_QUESTIONS = ROOT / "eval/questions.yaml"
DEFAULT_MANIFEST = ROOT / "eval/datasets/hk_background_jobs.json"

# Define current agent capabilities.
# Used to mark scenarios as skipped if they require unsupported features.
SUPPORTED_REQUIREMENTS = {"memory"}
SUPPORTED_TOOLS = {"describe_dataset", "describe_column", "value_counts", "run_sql"}

# Terminal failures published by the graph when the model or the verifier API
# is unavailable. Ordinary rejection/clarification notices do not match them.
TERMINAL_ERROR_MARKERS = ("API Error:", "Ошибка проверки ответа")
INPUT_ERROR_REASON = "Input validation happens in the UI/API before the agent graph"

NUMBER_RE = re.compile(r"(?<![\w.,-])-?\d+(?:[.,]\d+)?(?![\w.,])")
DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


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


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


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
    for raw in NUMBER_RE.findall(DATETIME_RE.sub(" ", text)):
        number = Decimal(raw.replace(",", "."))
        if number.is_finite():
            yield number


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


def _text_datetimes(text: str) -> Iterator[datetime]:
    for raw in DATETIME_RE.findall(text):
        parsed = _parse_datetime(raw)
        if parsed is not None:
            yield parsed


def _ok_results(results: Sequence[Any]) -> Iterator[Any]:
    for result in results:
        if _field(result, "status") == "ok":
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
            yield from _walk(values)


def _structured_numbers(results: Sequence[Any]) -> Iterator[Decimal]:
    for value in _structured_scalars(results):
        number = _decimal(value)
        if number is not None:
            yield number


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


def _text_matches(expected: Any, text: str) -> bool:
    expected_number = _decimal(expected)
    if expected_number is not None:
        return any(number == expected_number for number in _text_numbers(text))
    if isinstance(expected, str):
        expected_datetime = _parse_datetime(expected)
        if expected_datetime is not None:
            return any(_same_instant(expected_datetime, actual) for actual in _text_datetimes(text))
        return expected in text
    return False


def _list_matches(expected: list[Any], results: Sequence[Any], *, unordered: bool) -> bool:
    """Match a reference list against whole tool collections, keeping tuple pairing."""
    scalar_items = all(not isinstance(item, (list, tuple, dict)) for item in expected)
    if scalar_items:
        expected_counts = Counter(map(_canonical, expected))
        for group in _structured_groups(results):
            flat = list(_walk(group))
            if unordered and Counter(map(_canonical, flat)) == expected_counts:
                return True
            if not unordered and [_canonical(item) for item in flat] == [
                _canonical(item) for item in expected
            ]:
                return True
        return False

    expected_rows = [_canonical(item) for item in expected]
    for group in _structured_groups(results):
        rows = [_canonical(row) for row in group]
        if unordered and Counter(rows) == Counter(expected_rows):
            return True
        if not unordered and rows == expected_rows:
            return True
    return False


def check_fact(fact: dict, answer_text: str, results: Sequence[Any]) -> bool:
    """Check one reference fact with its comparison rule, never by substring search."""
    expected = fact["value"]
    comparison = fact["comparison"]

    if comparison == "approx":
        target = _decimal(expected)
        if target is None:
            return False
        tolerance = Decimal(str(fact["abs_tolerance"]))
        return any(
            abs(number - target) <= tolerance
            for number in (*_structured_numbers(results), *_text_numbers(answer_text))
        )

    if isinstance(expected, list):
        if comparison == "exact_unordered":
            if _list_matches(expected, results, unordered=True):
                return True
            # A collection of non-numeric names (e.g. columns) may be listed in prose.
            scalar_items = all(not isinstance(item, (list, tuple, dict)) for item in expected)
            return (
                scalar_items
                and all(_decimal(item) is None for item in expected)
                and all(str(item) in answer_text for item in expected)
            )
        return _list_matches(expected, results, unordered=False)

    if comparison == "exact_unordered":
        return False
    return any(
        _scalar_matches(expected, value) for value in _structured_scalars(results)
    ) or _text_matches(expected, answer_text)


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
    return " ".join(
        text
        for block in blocks
        if _field(block, "type") == "text" and (text := _field(block, "markdown"))
    )


def evaluate_case(case: dict, result: dict) -> tuple[str, list[str], dict[str, Any]]:
    """Score one graph result; terminal failures are rejected before facts/intent."""
    expect = case.get("expect", {})
    outcome = expect.get("outcome")
    blocks = list(result.get("answer_blocks") or [])
    results = [item for item in (result.get("results") or []) if isinstance(item, ToolResult)]
    plan = list(result.get("plan") or [])
    text = _block_text(blocks)
    used_tools = [tool for item in results if (tool := _field(item, "tool"))]
    details: dict[str, Any] = {
        "answer": text,
        "used_tools": used_tools,
        "fact_checks": [],
    }

    issues = _terminal_issues(blocks, results, plan)
    if issues:
        return "failed", issues, details

    if outcome == "answer":
        tools_any = expect.get("tools_any") or []
        if tools_any and not set(tools_any).intersection(used_tools):
            issues.append(f"Did not use any expected tools {tools_any}. Used: {used_tools}")
        if not text.strip():
            issues.append("The answer contains no text block")

        for fact in expect.get("facts") or []:
            passed = check_fact(fact, text, results)
            details["fact_checks"].append(
                {
                    "key": fact["key"],
                    "comparison": fact["comparison"],
                    "expected": fact["value"],
                    "passed": passed,
                }
            )
            if not passed:
                issues.append(f"Fact '{fact['key']}' (value: {fact['value']}) not confirmed")

    elif outcome == "clarify":
        intent = _intent(result)
        if intent != expect.get("intent"):
            issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not any(_field(block, "type") == "clarification" for block in blocks):
            issues.append("Clarification outcome produced no clarification block")

    elif outcome == "reject":
        intent = _intent(result)
        if intent != expect.get("intent"):
            issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
        elif not blocks:
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
    unsupported_tools = set(expect.get("tools_any") or []) - SUPPORTED_TOOLS
    if unsupported_tools:
        return f"Unsupported tools: {sorted(unsupported_tools)}"
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
    config = {
        "configurable": {
            "thread_id": thread_id,
            "client": client,
            "catalog_loader": dataset.load_catalog,
            "tool_dispatcher": dataset.dispatch,
        }
    }
    return graph.invoke(state, config=config)


def process_case(
    case: dict,
    cases_by_id: dict[str, dict],
    client: Any,
    graph: Any,
    dataset: GoldenDataset | Any,
) -> dict[str, Any]:
    """Run prerequisites and the case in one chat thread, then score the case."""
    case_id = case["id"]
    skip_reason = _case_skip_reason(case)
    if skip_reason is not None:
        return {"id": case_id, "status": "skipped", "reason": skip_reason}

    thread_id = f"eval_{case_id}"
    history: list[Message] = []
    executed_context: list[str] = []

    try:
        dependencies = _dependency_order(case, cases_by_id)
    except ValueError as error:
        return {
            "id": case_id,
            "status": "blocked",
            "reason": str(error),
            "thread_id": thread_id,
        }

    for dependency in dependencies:
        dependency_id = dependency["id"]
        dependency_skip = _case_skip_reason(dependency)
        if dependency_skip is not None:
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} is skipped: {dependency_skip}",
                "thread_id": thread_id,
                "context_cases": executed_context,
            }
        executed_context.append(dependency_id)
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
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} failed to run: {error}",
                "thread_id": thread_id,
                "context_cases": executed_context,
            }
        status, issues, details = evaluate_case(dependency, dependency_result)
        if status != "passed":
            return {
                "id": case_id,
                "status": "blocked",
                "reason": f"Prerequisite {dependency_id} {status}: {'; '.join(issues)}",
                "thread_id": thread_id,
                "context_cases": executed_context,
            }
        history.extend(
            [
                Message(role="user", content=dependency.get("question", "")),
                Message(role="assistant", content=details["answer"]),
            ]
        )

    start_time = time.time()
    try:
        result = _invoke(graph, dataset, client, thread_id, history, case.get("question", ""))
    except Exception as error:  # noqa: BLE001 - report the failed run, not a crash
        return {
            "id": case_id,
            "status": "failed",
            "duration_seconds": round(time.time() - start_time, 2),
            "thread_id": thread_id,
            "context_cases": executed_context,
            "issues": [f"Graph execution failed: {error}"],
        }

    duration = time.time() - start_time
    status, issues, details = evaluate_case(case, result)
    return {
        "id": case_id,
        "status": status,
        "duration_seconds": round(duration, 2),
        "thread_id": thread_id,
        "context_cases": executed_context,
        "used_tools": details["used_tools"],
        "issues": issues,
        "answer": details["answer"],
        "fact_checks": details["fact_checks"],
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
    with open(questions_path, encoding="utf-8") as f:
        cases = yaml.load(f, Loader=UniqueKeyLoader)
    if not isinstance(cases, list):
        raise ValueError("Questions must be a YAML list")

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases_by_id = {case["id"]: case for case in cases}
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
        "schema_version": 1,
        "dataset": manifest.get("dataset_id"),
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "blocked": 0},
        "cases": [None] * len(cases),
    }

    print(f"Starting evaluation of {len(cases)} cases ({len(runnable)} runnable)...")
    workers = max(1, min(max_workers, len(runnable) or 1))
    try:
        for index, case in enumerate(cases):
            skip_reason = _case_skip_reason(case)
            if skip_reason is not None:
                report["cases"][index] = {
                    "id": case["id"],
                    "question": case.get("question", ""),
                    "status": "skipped",
                    "reason": skip_reason,
                }
                report["summary"]["skipped"] += 1

        if blocked_reason is not None:
            for index, case in enumerate(cases):
                if _case_skip_reason(case) is None:
                    report["cases"][index] = {
                        "id": case["id"],
                        "question": case.get("question", ""),
                        "status": "blocked",
                        "reason": blocked_reason,
                    }
                    report["summary"]["blocked"] += 1
                    report["summary"]["total"] += 1
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
                    report["cases"][index] = result
                    report["summary"][result["status"]] += 1
                    if result["status"] != "skipped":
                        report["summary"]["total"] += 1
                    print(f"Completed case: {result['id']} -> {result['status']}")
    finally:
        if owns_dataset and dataset is not None:
            dataset.close()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)

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
