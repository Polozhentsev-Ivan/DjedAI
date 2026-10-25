"""Повторяемый замер режимов reasoning, схем агента, SQL и полного хода.

Данные синтетические, пользовательские данные не отправляются. Каждый вызов
модели помечен стадией, поэтому отчёт разделяет время стадий и общее время
полного хода; время одной стадии не выдаётся за время ответа.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from djedai.agent.client import BIModelClient  # noqa: E402
from djedai.agent.diagnostics import RunDiagnostics  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.agent.prompts import load_prompt  # noqa: E402
from djedai.agent.reasoning import (  # noqa: E402
    PROVIDER_DEFAULT,
    REASONING_EFFORTS,
    REASONING_STAGES,
    actual_reasoning_effort,
    normalize_reasoning_profile,
)
from djedai.analytics.registry import execute_tool, get_tools_for_llm  # noqa: E402
from djedai.config import ReasoningEffort, get_limits  # noqa: E402
from djedai.contracts.agent import (  # noqa: E402
    ComposeResponse,
    Message,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)
from djedai.contracts.tool_result import ToolResult  # noqa: E402
from djedai.execution import ExecutionBudget, TurnTimeout  # noqa: E402

# Fixed synthetic dataset. Expectations below are computed by hand from these
# rows, not by the SQL under test.
ORDERS = (
    (1, 1, "A", Decimal("10.00"), "RUB", Decimal("0")),
    (2, 2, "A", Decimal("20.00"), "RUB", Decimal("10")),
    (3, 3, "B", Decimal("5.00"), "USD", Decimal("0")),
    (4, 4, "C", Decimal("40.00"), "RUB", Decimal("50")),
)
CUSTOMERS = ((1, "retail"), (2, "wholesale"), (3, "retail"), (4, "retail"))
REFUNDS: tuple[tuple[Any, ...], ...] = ()
SALES = (("A", Decimal("100.00"), Decimal("80.00")),)

CATALOG = """Таблицы датасета benchmark.
orders(order_id INTEGER, customer_id INTEGER, region VARCHAR, amount DECIMAL(12,2),
       currency VARCHAR, discount_pct DECIMAL(5,2)) — 4 заказа.
customers(customer_id INTEGER, segment VARCHAR) — 4 клиента.
refunds(order_id INTEGER, amount DECIMAL(12,2)) — пустая таблица возвратов.
sales(region VARCHAR, gross_sales DECIMAL(12,2), net_sales DECIMAL(12,2)) — продажи,
       где gross_sales и net_sales являются разными метриками без выбранной по умолчанию.
"""

EXPECTED_COUNT = ((4,),)
EXPECTED_SUM_REGION = (("A", Decimal("30")), ("B", Decimal("5")), ("C", Decimal("40")))
EXPECTED_FILTER_JOIN = (("retail", Decimal("50")), ("wholesale", Decimal("20")))
EXPECTED_CURRENCY = (("RUB", Decimal("70")), ("USD", Decimal("5")))
EXPECTED_PERCENTAGE = ((Decimal("50"),),)

_NUMBER = re.compile(r"(?<![\w.,])-?\d+(?:[.,]\d+)?(?!\w|[.,]\d)")


@dataclass(frozen=True)
class StageCase:
    name: str
    stage: str
    schema: type
    system: str
    question: str
    expected: str
    expected_rows: tuple[tuple[Any, ...], ...] | None = None
    expected_numbers: tuple[str, ...] = ()
    forbidden_numbers: tuple[str, ...] = ()
    expect_no_numbers: bool = False
    expected_bindings: tuple[tuple[str, str], ...] = ()
    expected_tables: tuple[str, ...] = ()
    ordered_rows: bool = False


@dataclass(frozen=True)
class TurnCase:
    name: str
    question: str
    expected: str
    expected_rows: tuple[tuple[Any, ...], ...] | None = None
    expected_numbers: tuple[str, ...] = ()
    forbidden_numbers: tuple[str, ...] = ()
    expect_no_numbers: bool = False
    requires_chart: bool = False
    expected_bindings: tuple[tuple[str, str], ...] = ()
    ordered_rows: bool = False


def _tool_registry() -> str:
    return "\n".join(
        f"- {tool['name']}: {tool['description']} \n  "
        f"Schema: {json.dumps(tool['parameters_schema'])}"
        for tool in get_tools_for_llm()
    )


def _understand_system() -> str:
    return load_prompt("understand").format(catalog=CATALOG, history="")


def _plan_system() -> str:
    return load_prompt("plan").format(
        catalog=CATALOG, results="", tools_registry=_tool_registry(), history=""
    )


def _facts(columns: list[str], rows: list[list[Any]]) -> str:
    return str([{"tool": "run_sql", "data": {"columns": columns, "rows": rows}}])


def _compose_system(facts: str) -> str:
    return load_prompt("compose").format(facts=facts)


def _verify_system(facts: str, draft: str) -> str:
    return load_prompt("verify").format(facts=facts, draft=draft)


def _visualize_system(query: str, rows: list[list[Any]]) -> str:
    summary = [
        {
            "result_id": "r_benchmark",
            "tool": "run_sql",
            "columns": ["region", "total"],
            "sample_rows": rows,
            "chart_hint": {"type": "bar", "x": "region", "y": ["total"]},
            "facts": [],
        }
    ]
    return load_prompt("visualize").format(
        query=query, results_summary=json.dumps(summary, ensure_ascii=False)
    )


COUNT_FACTS = _facts(["n"], [[4]])
CURRENCY_FACTS = _facts(["currency", "total"], [["RUB", 70], ["USD", 5]])
PERCENTAGE_FACTS = _facts(["pct"], [[50]])
MISSING_FACTS = _facts(["total"], [[None]])
REGION_FACTS = _facts(["region", "total"], [["A", 30], ["B", 5], ["C", 40]])

CASES: tuple[StageCase, ...] = (
    StageCase(
        "understand_count",
        "understand",
        Understanding,
        _understand_system(),
        "Сколько строк в orders?",
        "data_question",
        expected_tables=("orders",),
    ),
    StageCase(
        "understand_ambiguous",
        "understand",
        Understanding,
        _understand_system(),
        "Какие у нас продажи?",
        "ambiguous",
    ),
    StageCase(
        "plan_count",
        "plan",
        PlanResponse,
        _plan_system(),
        "Подсчитай COUNT(*) всех строк orders.",
        "count",
        expected_rows=EXPECTED_COUNT,
    ),
    StageCase(
        "plan_sum_region",
        "plan",
        PlanResponse,
        _plan_system(),
        "Сумма amount по region в orders. Сортировка по region по возрастанию.",
        "sum_region",
        expected_rows=EXPECTED_SUM_REGION,
        ordered_rows=True,
    ),
    StageCase(
        "plan_filter_join",
        "plan",
        PlanResponse,
        _plan_system(),
        "Сумма amount по segment клиентов для заказов с amount > 5. "
        "Сортировка по segment по возрастанию.",
        "filter_join",
        expected_rows=EXPECTED_FILTER_JOIN,
        ordered_rows=True,
    ),
    StageCase(
        "plan_missing",
        "plan",
        PlanResponse,
        _plan_system(),
        "Сумма возвратов из таблицы refunds.",
        "missing",
    ),
    StageCase(
        "plan_currency",
        "plan",
        PlanResponse,
        _plan_system(),
        "Сумма amount по currency в orders. Сортировка по currency по возрастанию.",
        "currency",
        expected_rows=EXPECTED_CURRENCY,
        ordered_rows=True,
    ),
    StageCase(
        "plan_percentage",
        "plan",
        PlanResponse,
        _plan_system(),
        "Какой процент заказов имеет discount_pct > 0? Верни один показатель.",
        "percentage",
        expected_rows=EXPECTED_PERCENTAGE,
    ),
    StageCase(
        "compose_count",
        "compose",
        ComposeResponse,
        _compose_system(COUNT_FACTS),
        "Сколько строк? Ответь только числом в одном текстовом блоке.",
        "count",
        expected_numbers=("4",),
    ),
    StageCase(
        "compose_currency",
        "compose",
        ComposeResponse,
        _compose_system(CURRENCY_FACTS),
        "Назови суммы по валютам.",
        "currency",
        expected_numbers=("70", "5"),
        expected_bindings=(("RUB", "70"), ("USD", "5")),
    ),
    StageCase(
        "compose_percentage",
        "compose",
        ComposeResponse,
        _compose_system(PERCENTAGE_FACTS),
        "Какой процент заказов со скидкой? Ответь числом.",
        "percentage",
        expected_numbers=("50",),
    ),
    StageCase(
        "compose_missing",
        "compose",
        ComposeResponse,
        _compose_system(MISSING_FACTS),
        "Сообщи об отсутствии данных, не придумывая числа.",
        "missing",
        expect_no_numbers=True,
    ),
    StageCase(
        "verify_valid",
        "verify",
        VerifyResponse,
        _verify_system(COUNT_FACTS, "Всего 4 строки."),
        "Verify the numbers and formats.",
        "valid",
    ),
    StageCase(
        "verify_invalid",
        "verify",
        VerifyResponse,
        _verify_system(COUNT_FACTS, "Всего 5 строк."),
        "Verify the numbers and formats.",
        "corrected",
        expected_numbers=("4",),
        forbidden_numbers=("5",),
    ),
    StageCase(
        "visualize_region",
        "visualize",
        VisualizeResponse,
        _visualize_system(
            "Построй график суммы amount по region.", [["A", 30], ["B", 5], ["C", 40]]
        ),
        "Построй график суммы amount по region.",
        "chart",
    ),
)

TURN_CASES: tuple[TurnCase, ...] = (
    TurnCase(
        "turn_count",
        "Сколько строк в orders? Ответь только числом.",
        "count",
        EXPECTED_COUNT,
        ("4",),
    ),
    TurnCase(
        "turn_sum_region",
        "Сумма amount по region в orders. Сортировка по region по возрастанию.",
        "sum_region",
        EXPECTED_SUM_REGION,
        ("30", "5", "40"),
        expected_bindings=(("A", "30"), ("B", "5"), ("C", "40")),
        ordered_rows=True,
    ),
    TurnCase(
        "turn_filter_join",
        "Сумма amount по segment клиентов для заказов с amount > 5. "
        "Сортировка по segment по возрастанию.",
        "filter_join",
        EXPECTED_FILTER_JOIN,
        ("50", "20"),
        expected_bindings=(("retail", "50"), ("wholesale", "20")),
        ordered_rows=True,
    ),
    TurnCase(
        "turn_missing",
        "Сумма возвратов из таблицы refunds.",
        "missing",
        expect_no_numbers=True,
    ),
    TurnCase(
        "turn_currency",
        "Сумма amount по currency в orders. Сортировка по currency по возрастанию.",
        "currency",
        EXPECTED_CURRENCY,
        ("70", "5"),
        expected_bindings=(("RUB", "70"), ("USD", "5")),
        ordered_rows=True,
    ),
    TurnCase(
        "turn_percentage",
        "Какой процент заказов имеет discount_pct > 0? Верни один показатель.",
        "percentage",
        EXPECTED_PERCENTAGE,
        ("50",),
    ),
    TurnCase(
        "turn_ambiguous",
        "Какие у нас продажи?",
        "ambiguous",
        expect_no_numbers=True,
    ),
    TurnCase(
        "turn_chart",
        "Покажи график суммы amount по region.",
        "chart",
        EXPECTED_SUM_REGION,
        ("30", "5", "40"),
        requires_chart=True,
    ),
)


def make_connection() -> duckdb.DuckDBPyConnection:
    """Create the fixed benchmark dataset in an isolated in-memory DuckDB."""
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE orders(order_id INTEGER, customer_id INTEGER, region VARCHAR, "
        "amount DECIMAL(12,2), currency VARCHAR, discount_pct DECIMAL(5,2))"
    )
    connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ORDERS)
    connection.execute("CREATE TABLE customers(customer_id INTEGER, segment VARCHAR)")
    connection.executemany("INSERT INTO customers VALUES (?, ?)", CUSTOMERS)
    connection.execute("CREATE TABLE refunds(order_id INTEGER, amount DECIMAL(12,2))")
    if REFUNDS:
        connection.executemany("INSERT INTO refunds VALUES (?, ?)", REFUNDS)
    connection.execute(
        "CREATE TABLE sales(region VARCHAR, gross_sales DECIMAL(12,2), net_sales DECIMAL(12,2))"
    )
    connection.executemany("INSERT INTO sales VALUES (?, ?, ?)", SALES)
    return connection


def _number_key(value: str) -> Decimal:
    return Decimal(value.replace(",", "."))


def numbers_in_text(text: str) -> set[Decimal]:
    """Extract numeric tokens for an independent comparison with expectations."""
    numbers = set()
    for match in _NUMBER.finditer(text.replace("\N{MINUS SIGN}", "-")):
        try:
            numbers.add(_number_key(match.group()))
        except InvalidOperation:
            continue
    return numbers


def _normalized(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, float):
        return Decimal(str(value)).normalize()
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value).normalize()
    return value


def rows_match(
    actual: list[list[Any]], expected: tuple[tuple[Any, ...], ...], *, ordered: bool = True
) -> bool:
    if len(actual) != len(expected):
        return False
    left = [tuple(_normalized(value) for value in row) for row in actual]
    right = [tuple(_normalized(value) for value in row) for row in expected]
    return left == right if ordered else Counter(left) == Counter(right)


def _missing_data(result: ToolResult) -> bool:
    if result.status != "ok" or result.data is None:
        return False
    return not result.data.rows or all(value is None for row in result.data.rows for value in row)


def _plan_results(plan: list[Any]) -> list[ToolResult]:
    if not plan:
        return []
    results = []
    with make_connection() as connection:
        for step in plan:
            try:
                results.append(execute_tool(step.tool, step.args, connection=connection))
            except Exception:
                return []
    return results


def _final_result_matches(result: ToolResult, case: StageCase | TurnCase) -> bool:
    if case.expected == "missing":
        return _missing_data(result)
    if case.expected_rows is None:
        return False
    return (
        result.status == "ok"
        and result.data is not None
        and rows_match(result.data.rows, case.expected_rows, ordered=case.ordered_rows)
    )


def plan_ok(plan: list[Any], case: StageCase | TurnCase) -> bool:
    results = _plan_results(plan)
    # The published answer comes from the last tool result; an intermediate
    # correct query must not hide a wrong final one.
    return bool(results) and _final_result_matches(results[-1], case)


def text_ok(text: str, case: StageCase | TurnCase) -> bool:
    text = text.replace("\N{MINUS SIGN}", "-").strip()
    if not text:
        return False
    numbers = numbers_in_text(text)
    if case.expect_no_numbers:
        return not numbers and bool(
            re.search(
                r"нет\s+данных|данн\w*\s+нет|данн\w*\s+отсутств\w*|"
                r"отсутств\w*\s+данн\w*|пуст\w*\s+(?:таблиц|выборк|результат)|"
                r"не\s+вернул|no\s+(?:data|rows)|empty",
                text,
                re.I,
            )
        )
    expected = {_number_key(number) for number in case.expected_numbers}
    forbidden = {_number_key(number) for number in case.forbidden_numbers}
    if numbers != expected or forbidden & numbers:
        return False
    if case.expected_bindings:
        return _bindings_match(text, case.expected_bindings)
    if re.search(r"\b(?:RUB|USD|руб\w*|доллар\w*)\b|[₽$]", text, re.I):
        return False
    scalar = text.strip("`*_ .!\n")
    if case.expected in {"count", "corrected"}:
        return not re.search(r"%|процент", text, re.I) and (
            bool(_NUMBER.fullmatch(scalar))
            or bool(re.search(r"строк|запис|заказ|количество|\bcount\b|\brows?\b", text, re.I))
        )
    if case.expected == "percentage":
        return bool(_NUMBER.fullmatch(scalar)) or bool(re.search(r"%|процент", text, re.I))
    return True


def _bindings_match(text: str, bindings: tuple[tuple[str, str], ...]) -> bool:
    """Check labeled numeric claims, including duplicates, without swapping categories.

    Supported benchmark answers use label/value pairs (either order). Unknown
    prose/extra numeric claims are rejected rather than treated as proved.
    """
    covered: set[int] = set()
    separator = r"[\t \u00a0\u202f:*_=|()\[\]—–]{0,24}"
    after = re.compile(rf"{separator}(?P<value>{_NUMBER.pattern})")
    before = re.compile(rf"(?P<value>{_NUMBER.pattern}){separator}$")
    for label, expected in bindings:
        label_pattern = rf"(?<!\w){re.escape(label)}(?!\w)"
        labels = list(re.finditer(label_pattern, text, re.I))
        if not labels:
            return False
        for match in labels:
            # A label's own following value wins over the preceding row/pair.
            value = after.match(text, match.end()) or before.search(text[: match.start()])
            if value is None or _number_key(value.group("value")) != _number_key(expected):
                return False
            covered.update(range(*value.span("value")))
    return all(set(range(*match.span())) <= covered for match in _NUMBER.finditer(text))


def semantic_ok(data: Any, case: StageCase) -> bool:
    if isinstance(data, Understanding):
        if case.expected == "ambiguous":
            clarification = data.clarification
            labels = [
                {
                    name
                    for name, pattern in [
                        ("gross", r"gross_sales|валов\w*"),
                        ("net", r"net_sales|чист\w*"),
                    ]
                    if re.search(pattern, option, re.I)
                }
                for option in (clarification.options if clarification else [])
            ]
            return bool(
                data.intent == "ambiguous"
                and clarification
                and clarification.question.strip()
                and len(labels) == 2
                and {frozenset(label) for label in labels}
                == {frozenset({"gross"}), frozenset({"net"})}
            )
        columns = {"order_id", "customer_id", "region", "amount", "currency", "discount_pct"}
        columns |= {"orders." + name for name in columns}
        return (
            data.intent == "data_question"
            and set(data.entities.tables) == set(case.expected_tables)
            and all(column in columns for column in data.entities.columns)
            and not data.references
        )
    if isinstance(data, PlanResponse):
        return plan_ok(data.plan, case)
    if isinstance(data, ComposeResponse):
        text = "\n".join(block.markdown or "" for block in data.answer_blocks).strip()
        return text_ok(text, case)
    if isinstance(data, VerifyResponse):
        if case.expected == "valid":
            return data.is_valid
        if case.expected == "corrected":
            return not data.is_valid and text_ok(data.corrected_markdown or "", case)
        return False
    if isinstance(data, VisualizeResponse):
        source = ToolResult(
            result_id="r_benchmark",
            tool="run_sql",
            status="ok",
            data={
                "columns": ["region", "total"],
                "rows": [list(row) for row in EXPECTED_SUM_REGION],
            },
        )
        return charts_ok(data.charts, [source], EXPECTED_SUM_REGION)
    return False


def charts_ok(
    charts: list[Any], results: list[ToolResult], expected: tuple[tuple[Any, ...], ...]
) -> bool:
    """Validate chart sources/fields and the displayed labeled values independently."""
    if not charts:
        return False
    sources = {result.result_id: result for result in results if result.status == "ok"}
    for chart in charts:
        if chart.type not in {"bar", "line", "area", "pie"}:
            return False
        source = sources.get(chart.result_id)
        if not source or source.data is None or chart.x is None or len(chart.y) != 1:
            return False
        columns = source.data.columns
        fields = [chart.x.field, chart.y[0].field]
        optional = [chart.color.field] if chart.color else []
        if chart.sort:
            optional.append(chart.sort.field)
        if any(field not in columns for field in [*fields, *optional]):
            return False
        indexes = [columns.index(field) for field in fields]
        rows = [[row[index] for index in indexes] for row in source.data.rows]
        if not all(
            isinstance(row[1], (int, float, Decimal)) and not isinstance(row[1], bool)
            for row in rows
        ):
            return False
        if not rows_match(rows, expected, ordered=False):
            return False
    return True


def _reasoning_for(client: Any, stage: str) -> ReasoningEffort:
    method = getattr(client, "reasoning_for", None)
    if method is not None:
        return method(stage)
    profile = getattr(client, "reasoning", None) or {}
    return profile.get(stage, PROVIDER_DEFAULT)


def _latency(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "median": round(statistics.median(ordered), 3),
        "p95": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 3),
        "max": round(ordered[-1], 3),
    }


def _stage_latency(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["stage"], []).append(row["seconds"])
    return {stage: _latency(values) for stage, values in grouped.items()}


def _error_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        error = row.get("error_type")
        if error:
            counts[error] = counts.get(error, 0) + 1
    return counts


def run_benchmark(
    client: Any,
    *,
    repeats: int,
    context_lines: int,
    turn_repeats: int = 0,
    profile_name: str = "custom",
    cases: tuple[StageCase, ...] = CASES,
    turn_cases: tuple[TurnCase, ...] = TURN_CASES,
    turn_timeout_seconds: float | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    rows = []
    turns = []

    def checkpoint() -> None:
        if on_progress is not None:
            on_progress(
                {
                    "profile": profile_name,
                    "model": client.model,
                    "status": "running",
                    "calls": list(rows),
                    "turns": list(turns),
                }
            )

    checkpoint()
    selected = [(case, turn) for turn in range(repeats) for case in cases]
    if context_lines:
        padding = "\n".join(
            f"Служебная таблица extra_{n}: id VARCHAR, label VARCHAR." for n in range(context_lines)
        )
        selected.append(
            (
                StageCase(
                    "context_probe",
                    "understand",
                    Understanding,
                    _understand_system() + "\n" + padding,
                    "Сколько строк в orders?",
                    "data_question",
                    expected_tables=("orders",),
                ),
                0,
            )
        )
    for case, turn in selected:
        started = perf_counter()
        response = client.generate_typed_response(
            case.system, case.question, case.schema, stage=case.stage
        )
        elapsed = perf_counter() - started
        valid = response.status == "success" and response.data is not None
        correct = semantic_ok(response.data, case) if valid else False
        sql_ok = (
            plan_ok(response.data.plan, case)
            if valid and isinstance(response.data, PlanResponse)
            else None
        )
        text = ""
        if valid and isinstance(response.data, ComposeResponse):
            text = "\n".join(block.markdown or "" for block in response.data.answer_blocks)
        if valid and isinstance(response.data, VerifyResponse):
            text = response.data.corrected_markdown or ""
        numbers_ok = text_ok(text, case) if valid and text else None
        setting = _reasoning_for(client, case.stage)
        rows.append(
            {
                "case": case.name,
                "stage": case.stage,
                "repeat": turn + 1,
                "schema": case.schema.__name__,
                "seconds": round(elapsed, 3),
                "schema_valid": valid,
                "semantic_ok": correct,
                "sql_ok": sql_ok,
                "numbers_ok": numbers_ok,
                "error_type": response.error_type,
                "http_status": getattr(response, "http_status", None),
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "finish_reason": response.finish_reason,
                "reasoning_setting": setting,
                "reasoning_effort": actual_reasoning_effort(setting),
                "data": response.data.model_dump(mode="json") if valid else None,
                "invalid_model_output": response.raw_response if not valid else None,
            }
        )
        print(
            f"{client.model} [{profile_name}]: {case.name} {turn + 1}: "
            f"schema={valid} semantic={correct} {elapsed:.2f}s",
            flush=True,
        )
        checkpoint()

    graph = build_agent_graph() if turn_repeats and turn_cases else None
    if graph is not None:
        for turn_case in turn_cases:
            for repeat in range(turn_repeats):
                turn = run_turn(
                    graph,
                    client,
                    turn_case,
                    repeat=repeat,
                    profile_name=profile_name,
                    turn_timeout_seconds=turn_timeout_seconds,
                )
                turns.append(turn)
                print(
                    f"{client.model} [{profile_name}]: {turn_case.name} {repeat + 1}: "
                    f"schema={turn['schema_valid']} semantic={turn['semantic_ok']} "
                    f"total={turn['total_seconds']:.2f}s",
                    flush=True,
                )
                checkpoint()

    all_rows = [*rows, *turns]
    prompt_tokens = [row["prompt_tokens"] for row in rows if row["prompt_tokens"] is not None]
    prompt_tokens += [row["input_tokens"] for row in turns if row["input_tokens"] is not None]
    return {
        "status": "completed",
        "profile": profile_name,
        "model": client.model,
        "reasoning": {stage: _reasoning_for(client, stage) for stage in REASONING_STAGES},
        "requests": len(all_rows),
        "schema_valid": sum(row["schema_valid"] for row in all_rows),
        "semantic_ok": sum(row["semantic_ok"] for row in all_rows),
        "errors": _error_counts(all_rows),
        "timeouts": sum(1 for row in all_rows if row.get("error_type") == "TIMEOUT"),
        "latency_seconds": _latency([row["seconds"] for row in rows]),
        "stage_latency_seconds": _stage_latency(rows),
        "turn_latency_seconds": _latency([row["total_seconds"] for row in turns]),
        "turn_stage_latency_seconds": _turn_stage_latency(turns),
        "max_prompt_tokens": max(prompt_tokens, default=0),
        "calls": rows,
        "turns": turns,
        "stage_usage": usage_totals(rows, "prompt_tokens", "completion_tokens"),
        "turn_usage": usage_totals(turns, "input_tokens", "output_tokens"),
    }


def usage_totals(rows: list[dict[str, Any]], incoming: str, outgoing: str) -> dict[str, Any]:
    """Known partial totals are labeled; an incomplete full total remains null."""
    result: dict[str, Any] = {"calls": len(rows)}
    for key, field in [("input_tokens", incoming), ("output_tokens", outgoing)]:
        values = [row[field] for row in rows]
        known = sum(value for value in values if value is not None)
        missing = sum(value is None for value in values)
        result[key] = known if not missing else None
        result["known_" + key] = known
        result["missing_" + key] = missing
    result["complete"] = all(
        result[key] == 0 for key in ["missing_input_tokens", "missing_output_tokens"]
    )
    return result


def _turn_stage_latency(turns: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, list[float]] = {}
    for turn in turns:
        for stage, seconds in turn["stage_seconds"].items():
            grouped.setdefault(stage, []).append(seconds)
    return {stage: _latency(values) for stage, values in grouped.items()}


def _answer_text(state: dict[str, Any]) -> str:
    return "\n".join(
        block.markdown or "" for block in state.get("answer_blocks", []) if block.type == "text"
    )


def _turn_stage_seconds(events: list[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for event in events:
        if event["event"] == "stage_finished":
            grouped[event["node"]] = grouped.get(event["node"], 0.0) + (event["duration_ms"] / 1000)
    return {stage: round(seconds, 3) for stage, seconds in grouped.items()}


def _turn_tokens(events: list[dict[str, Any]]) -> tuple[int | None, int | None, int | None]:
    calls = [event for event in events if event["event"] == "model_call_finished"]
    if not calls:
        return None, None, None

    def total(field: str) -> int | None:
        values = [call[field] for call in calls]
        # A partial sum would understate the turn: keep null when any call lacks usage.
        if any(value is None for value in values):
            return None
        return sum(values)

    return total("input_tokens"), total("output_tokens"), total("reasoning_tokens")


def _turn_model_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "stage": event["stage"] or event["node"],
            "schema": event["schema"],
            "status": event["status"],
            "seconds": round(event["duration_ms"] / 1000, 3),
            "input_tokens": event["input_tokens"],
            "output_tokens": event["output_tokens"],
            "reasoning_tokens": event["reasoning_tokens"],
            "reasoning_setting": event["reasoning_setting"],
            "reasoning_effort": event["reasoning_effort"],
            "attempts": event["attempts"],
        }
        for event in events
        if event["event"] == "model_call_finished"
    ]


_STAGE_SCHEMAS = {
    "understand": "Understanding",
    "plan": "PlanResponse",
    "visualize": "VisualizeResponse",
    "compose": "ComposeResponse",
    "verify": "VerifyResponse",
}


def _synthesized_model_calls(events: list[dict[str, Any]], client: Any) -> list[dict[str, Any]]:
    """Reconstruct model calls from stage timing for clients without diagnostics.

    Real ``BIModelClient`` calls emit ``model_call_finished`` and always use that
    path. Test doubles and other clients that bypass the transport still let the
    benchmark check stage coverage, so fall back to graph stage events rather
    than treating every turn as schema-invalid.
    """
    calls = []
    for event in events:
        if event["event"] != "stage_finished":
            continue
        stage = event["node"]
        if stage not in _STAGE_SCHEMAS:
            continue
        setting = _reasoning_for(client, stage)
        calls.append(
            {
                "stage": stage,
                "schema": _STAGE_SCHEMAS[stage],
                "status": "success" if event["error_type"] is None else "error",
                "seconds": round(event["duration_ms"] / 1000, 3),
                "input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "reasoning_setting": setting,
                "reasoning_effort": actual_reasoning_effort(setting),
            }
        )
    return calls


def _expected_turn_stages(case: TurnCase) -> set[str]:
    if case.expected == "ambiguous":
        return {"understand"}
    if case.requires_chart:
        return {"understand", "plan", "visualize", "compose", "verify"}
    return {"understand", "plan", "compose", "verify"}


def _results_match(results: list[ToolResult], case: TurnCase) -> bool | None:
    if case.expected == "ambiguous":
        return None
    return bool(results) and _final_result_matches(results[-1], case)


def run_turn(
    graph: Any,
    client: Any,
    case: TurnCase,
    *,
    repeat: int,
    profile_name: str,
    turn_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    run_id = f"benchmark-{profile_name}-{case.name}-{repeat}"
    diagnostics = RunDiagnostics(
        events.append,
        owner_id="benchmark",
        chat_id=f"chat-{case.name}",
        dataset_id="benchmark",
        user_message_id=run_id,
        run_id=run_id,
    )

    class Catalog:
        def to_prompt(self) -> str:
            return CATALOG

    connection = make_connection()

    def dispatcher(tool: str, args: dict[str, Any]) -> ToolResult:
        return execute_tool(tool, args, connection=connection)

    config = {
        "configurable": {
            "thread_id": run_id,
            "client": client,
            "catalog_loader": lambda _dataset_id: Catalog(),
            "tool_dispatcher": dispatcher,
            "diagnostics": diagnostics,
            "budget": ExecutionBudget(
                get_limits().agent_turn_timeout_seconds
                if turn_timeout_seconds is None
                else turn_timeout_seconds
            ),
        }
    }
    state: dict[str, Any] = {}
    error_type: str | None = None
    started = perf_counter()
    try:
        state = graph.invoke(
            {
                "chat_id": f"chat-{case.name}",
                "dataset_id": "benchmark",
                "messages": [Message(role="user", content=case.question)],
            },
            config=config,
        )
    except Exception as exc:
        error_type = "TIMEOUT" if isinstance(exc, TurnTimeout) else type(exc).__name__
        try:
            state = graph.get_state(config).values or {}
        except Exception:
            pass
    total_seconds = perf_counter() - started
    if not diagnostics.finished:
        diagnostics.finish("error" if error_type else "success", error_type)
    connection.close()

    calls = _turn_model_calls(events) or _synthesized_model_calls(events, client)
    stages = {call["stage"] for call in calls}
    schema_valid = (
        error_type is None
        and bool(calls)
        and all(call["status"] == "success" for call in calls)
        and _expected_turn_stages(case) <= stages
    )
    text = _answer_text(state)
    understanding = state.get("understanding")
    if case.expected == "ambiguous":
        semantic = bool(
            understanding
            and understanding.intent == "ambiguous"
            and understanding.clarification is not None
        )
        numbers_ok = None
        sql_ok = None
    else:
        sql_ok = _results_match(state.get("results", []), case)
        numbers_ok = text_ok(text, case)
        semantic = bool(sql_ok) and bool(numbers_ok)
        if case.requires_chart:
            chart_valid = charts_ok(
                state.get("charts", []), state.get("results", []), case.expected_rows or ()
            )
            # A requested chart carries its values through result_id. Repeating
            # every plotted value in prose is optional; actual text claims still
            # must match the independent labeled expectations.
            chart_text_case = TurnCase(
                case.name,
                case.question,
                case.expected,
                case.expected_rows,
                case.expected_numbers,
                expected_bindings=(("A", "30"), ("B", "5"), ("C", "40")),
            )
            numbers_ok = not numbers_in_text(text) or text_ok(text, chart_text_case)
            semantic = bool(sql_ok) and bool(numbers_ok) and chart_valid
    if any(block.type == "error" for block in state.get("answer_blocks", [])):
        semantic = False

    input_tokens, output_tokens, reasoning_tokens = _turn_tokens(events)
    return {
        "case": case.name,
        "repeat": repeat + 1,
        "seconds": round(total_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "stage_seconds": _turn_stage_seconds(events),
        "schema_valid": schema_valid,
        "semantic_ok": semantic,
        "sql_ok": sql_ok,
        "numbers_ok": numbers_ok,
        "error_type": error_type or (diagnostics.last_error if not schema_valid else None),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "final_numbers": sorted(str(number) for number in numbers_in_text(text)),
        "expected_numbers": list(case.expected_numbers),
        "charts": len(state.get("charts", [])),
        "calls": calls,
        "trace": {
            "understanding": understanding.model_dump(mode="json") if understanding else None,
            "plan": [step.model_dump(mode="json") for step in state.get("plan", [])],
            "results": [result.model_dump(mode="json") for result in state.get("results", [])],
            "answer_blocks": [
                block.model_dump(mode="json") for block in state.get("answer_blocks", [])
            ],
            "charts": [chart.model_dump(mode="json") for chart in state.get("charts", [])],
            "is_verified": state.get("is_verified"),
        },
    }


def _case_scores(report: dict[str, Any], case_name: str, field: str) -> tuple[int, int]:
    rows = [row for row in [*report["calls"], *report["turns"]] if row["case"] == case_name]
    return sum(bool(row[field]) for row in rows), len(rows)


def compare_profiles(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare candidate profiles with the first (baseline) profile."""
    if len(reports) < 2:
        return {}
    baseline = reports[0]
    case_names = list(
        dict.fromkeys(
            row["case"] for report in reports for row in [*report["calls"], *report["turns"]]
        )
    )
    comparisons = []
    for candidate in reports[1:]:
        regressions = []
        schema_regressions = []
        initial_failures = []
        case_comparisons = []
        incomplete_cases = []
        for name in case_names:
            baseline_ok, baseline_n = _case_scores(baseline, name, "semantic_ok")
            candidate_ok, candidate_n = _case_scores(candidate, name, "semantic_ok")
            baseline_schema, _ = _case_scores(baseline, name, "schema_valid")
            candidate_schema, _ = _case_scores(candidate, name, "schema_valid")
            if not baseline_n or candidate_n != baseline_n:
                incomplete_cases.append(name)
            if baseline_n and candidate_n and candidate_ok * baseline_n < baseline_ok * candidate_n:
                regressions.append(name)
            if baseline_ok < baseline_n:
                initial_failures.append(name)
            if (
                baseline_n
                and candidate_n
                and candidate_schema * baseline_n < baseline_schema * candidate_n
            ):
                schema_regressions.append(name)
            case_comparisons.append(
                {
                    "case": name,
                    "baseline_passed": baseline_ok,
                    "baseline_n": baseline_n,
                    "candidate_passed": candidate_ok,
                    "candidate_n": candidate_n,
                    "baseline_schema_valid": baseline_schema,
                    "candidate_schema_valid": candidate_schema,
                }
            )
        safe = not regressions and not schema_regressions and not incomplete_cases
        comparisons.append(
            {
                "model": candidate["model"],
                "baseline_profile": baseline["profile"],
                "candidate_profile": candidate["profile"],
                "regressed_cases": regressions,
                "schema_regressed_cases": schema_regressions,
                "baseline_initial_failures": initial_failures,
                "baseline_median_total_seconds": baseline["turn_latency_seconds"]["median"],
                "candidate_median_total_seconds": candidate["turn_latency_seconds"]["median"],
                "case_comparisons": case_comparisons,
                "incomplete_cases": incomplete_cases,
                "safe_to_enable": safe,
                "recommendation": "candidate_ok" if safe else "keep_baseline",
            }
        )
    return {"comparisons": comparisons}


def parse_reasoning_overrides(
    values: list[str], parser: argparse.ArgumentParser
) -> dict[str, ReasoningEffort]:
    overrides: dict[str, ReasoningEffort] = {}
    for value in values:
        stage, separator, effort = value.partition("=")
        if not separator or stage not in REASONING_STAGES:
            parser.error(
                f"--reasoning ожидает STAGE=EFFORT, где STAGE: {', '.join(REASONING_STAGES)}"
            )
        if effort not in REASONING_EFFORTS:
            parser.error(
                f"--reasoning: недопустимый режим {effort!r}; "
                f"допустимые значения: {', '.join(sorted(REASONING_EFFORTS))}"
            )
        overrides[stage] = effort  # type: ignore[assignment]
    return overrides


PROFILE_PRESETS: dict[str, dict[str, ReasoningEffort]] = {
    "baseline": {stage: PROVIDER_DEFAULT for stage in REASONING_STAGES},
    # Conservative candidate: only cheap classification/visual selection loses
    # reasoning; planning, composition and verification keep provider default.
    "proposed": {
        "understand": "none",
        "plan": PROVIDER_DEFAULT,
        "visualize": "none",
        "compose": PROVIDER_DEFAULT,
        "verify": PROVIDER_DEFAULT,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--base-url", default="https://api.deepinfra.com/v1/openai")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--turn-repeats", type=int, default=3)
    parser.add_argument("--skip-turns", action="store_true")
    parser.add_argument("--context-lines", type=int, default=200)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--turn-timeout", type=float, default=None)
    parser.add_argument(
        "--reasoning-profile",
        action="append",
        default=None,
        help="baseline, proposed или custom; можно указать несколько раз",
    )
    parser.add_argument(
        "--reasoning",
        action="append",
        default=[],
        metavar="STAGE=EFFORT",
        help="переопределить режим стадии: provider_default|none|low|medium|high",
    )
    parser.add_argument(
        "--reasoning-supported",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="подтвердить, что endpoint/модель поддерживает reasoning_effort",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/model-profiles" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json",
    )
    args = parser.parse_args(argv)
    if (
        args.repeats < 1
        or args.turn_repeats < 0
        or args.context_lines < 0
        or args.max_output_tokens < 1
        or args.timeout <= 0
        or (args.turn_timeout is not None and args.turn_timeout <= 0)
    ):
        parser.error("Количество повторов, лимит ответа и timeout должны быть положительными")
    if args.skip_turns:
        args.turn_repeats = 0
    overrides = parse_reasoning_overrides(args.reasoning, parser)
    profile_names = args.reasoning_profile or ["baseline"]
    profiles: dict[str, dict[str, ReasoningEffort]] = {}
    for name in profile_names:
        if name == "custom":
            base: dict[str, ReasoningEffort] = {}
        elif name in PROFILE_PRESETS:
            base = PROFILE_PRESETS[name]
        else:
            parser.error("--reasoning-profile: допустимы baseline, proposed или custom")
        profiles[name] = normalize_reasoning_profile({**base, **overrides})
    if "baseline" in profiles:
        # Comparison always treats the first profile as the current production
        # baseline, regardless of the order supplied on the command line.
        profiles = {
            "baseline": profiles["baseline"],
            **{k: v for k, v in profiles.items() if k != "baseline"},
        }
    values = {**dotenv_values(args.env_file), **os.environ}
    key = values.get("DEEPINFRA_API_KEY") or values.get("MODEL_API_KEY")
    if not key:
        parser.error("Не найден ключ модели в окружении или .env")
    report: dict[str, Any] = {
        "report_version": 2,
        "status": "running",
        "source": source_fingerprint(),
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout,
        "repeats": args.repeats,
        "turn_repeats": args.turn_repeats,
        "context_lines": args.context_lines,
        "percentile_method": "nearest_rank: ceil(0.95*n), all outcomes included",
        "turn_timeout_seconds": args.turn_timeout
        if args.turn_timeout is not None
        else get_limits().agent_turn_timeout_seconds,
        "reasoning_supported": args.reasoning_supported,
        "profiles": profiles,
        "models": [],
        "comparison": {},
    }

    def save() -> None:
        report["updated_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(args.output)

    try:
        save()
        for profile_name, profile in profiles.items():
            for model in args.model:
                client = BIModelClient(
                    api_key=key,
                    base_url=args.base_url,
                    model_name=model,
                    timeout=args.timeout,
                    max_output_tokens=args.max_output_tokens,
                    reasoning=profile,
                    reasoning_supported=args.reasoning_supported,
                )
                position = len(report["models"])
                report["models"].append({})

                def progress(partial: dict[str, Any], index: int = position) -> None:
                    report["models"][index] = partial
                    save()

                try:
                    report["models"][position] = run_benchmark(
                        client,
                        repeats=args.repeats,
                        context_lines=args.context_lines,
                        turn_repeats=args.turn_repeats,
                        profile_name=profile_name,
                        turn_timeout_seconds=report["turn_timeout_seconds"],
                        on_progress=progress,
                    )
                finally:
                    client.client.close()
                save()
        comparisons = []
        for model in args.model:
            model_reports = [item for item in report["models"] if item["model"] == model]
            comparison = compare_profiles(model_reports)
            comparisons.extend(comparison.get("comparisons", []))
        report["comparison"] = {"comparisons": comparisons}
        report["status"] = "completed"
        save()
        return 0
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        save()
        return 130
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        save()
        detail = f": {error}" if isinstance(error, ValueError) else ""
        print(f"Benchmark failed: {type(error).__name__}{detail}", file=sys.stderr)
        return 1


def source_fingerprint() -> dict[str, Any]:
    files = [
        *sorted((ROOT / "src/djedai").rglob("*.py")),
        *sorted((ROOT / "prompts").glob("*.md")),
        Path(__file__),
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / ".python-version",
    ]
    hashes = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True, timeout=5
            )
        )
    except (OSError, subprocess.SubprocessError):
        sha, dirty = None, None
    fixture = json.dumps(
        {
            "orders": ORDERS,
            "customers": CUSTOMERS,
            "refunds": REFUNDS,
            "sales": SALES,
            "catalog": CATALOG,
        },
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )
    return {
        "application_sha": sha,
        "dirty": dirty,
        "file_sha256": hashes,
        "fixture_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
