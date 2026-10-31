"""Контрактные проверки каждого готового инструмента (#70).

Каждый инструмент в TOOLS_REGISTRY проходит одинаковый набор:
- успех → status="ok", ожидаемый fact_kind, chart_hint;
- pydantic-ограничения (Literal, ge, min_length) → ValidationError;
- ошибки на уровне функции (несуществующая колонка, agg без measure) →
  ToolResult(status="error", code=...).

Точечные тесты (`test_seasonality.py`, `test_correlation.py`, …) остаются —
они проверяют эталоны. Здесь — единый каркас для приёмки новых инструментов.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pytest
from pydantic import ValidationError

from djedai.analytics.registry import TOOLS_REGISTRY, execute_tool

# ---------- локальные фикстуры для инструментов с особыми таблицами ----------


@pytest.fixture
def contract_seasonal_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [
        (
            datetime(2021 + i // 12, i % 12 + 1, 1),
            1000.0 + 100.0 * math.cos(2 * math.pi * ((i % 12 + 1) - 7) / 12),
        )
        for i in range(36)
    ]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    yield con
    con.close()


@pytest.fixture
def contract_lagged_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(42)
    n = 180
    a = rng.normal(size=n)
    b = np.concatenate([np.zeros(5), a[:-5]]) + rng.normal(scale=0.1, size=n)
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)
    yield con
    con.close()


@pytest.fixture
def contract_corr_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (x DOUBLE, y DOUBLE)")
    rng = np.random.default_rng(0)
    x = rng.normal(size=200)
    y = 2.0 * x + rng.normal(scale=0.1, size=200)
    con.executemany(
        "INSERT INTO m VALUES (?, ?)",
        [(float(a), float(b)) for a, b in zip(x, y, strict=False)],
    )
    yield con
    con.close()


@pytest.fixture
def contract_compare_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (seg VARCHAR, value DOUBLE)")
    rng = np.random.default_rng(0)
    a_rows = [("A", float(10.0 + rng.normal())) for _ in range(50)]
    b_rows = [("B", float(15.0 + rng.normal())) for _ in range(50)]
    con.executemany("INSERT INTO m VALUES (?, ?)", a_rows + b_rows)
    yield con
    con.close()


@pytest.fixture
def contract_forecast_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [
        (
            datetime(2021 + i // 12, i % 12 + 1, 1),
            1000.0 + 50.0 * i + 100.0 * math.cos(2 * math.pi * ((i % 12 + 1) - 7) / 12),
        )
        for i in range(36)
    ]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    yield con
    con.close()


# ---------- success cases ----------
# (tool_name, kwargs, fact_kind, chart, fixture_name)
# fixture_name=None — использовать _get_mock_connection().

SUCCESS_CASES = [
    ("describe_dataset", {}, "dataset_summary", None, None),
    ("describe_column", {"table": "test", "column": "age"}, "numeric_profile", None, None),
    (
        "value_counts",
        {"table": "test", "column": "city"},
        "freq",
        ("bar", "city", ["count"]),
        None,
    ),
    ("run_sql", {"sql": "SELECT COUNT(*) AS n FROM test"}, "summary", None, None),
    (
        "time_aggregate",
        {"table": "events", "time_col": "ts", "freq": "month", "agg": "sum", "measure": "amount"},
        "time_summary",
        ("line", "time_bucket", ["value"]),
        None,
    ),
    (
        "find_duplicates",
        {"table": "test", "columns": ["city"]},
        "duplicate_summary",
        None,
        None,
    ),
    (
        "detect_anomalies",
        {"table": "metrics", "measure": "value", "method": "iqr", "id_column": "id"},
        "anomaly_summary",
        None,
        "conn",
    ),
    (
        "segment_profile",
        {"table": "test", "group_by": ["city"], "measures": ["age"]},
        "segment_summary",
        None,
        None,
    ),
    (
        "seasonality",
        {"table": "s", "time_col": "ts", "measure": "value", "freq": "month"},
        "seasonal_summary",
        ("line", "time_bucket", ["value", "trend", "seasonal"]),
        "contract_seasonal_conn",
    ),
    (
        "compare_segments",
        {"table": "m", "segment_col": "seg", "a": "A", "b": "B", "measures": ["value"]},
        "compare_summary",
        None,
        "contract_compare_conn",
    ),
    (
        "correlation",
        {"table": "m", "columns": ["x", "y"]},
        "metric",
        ("heatmap", "from", ["to"]),
        "contract_corr_conn",
    ),
    (
        "lagged_correlation",
        {"table": "s", "time_col": "ts", "a": "a", "b": "b", "freq": "day", "max_lag": 10},
        "metric",
        ("bar", "lag", ["r"]),
        "contract_lagged_conn",
    ),
    (
        "forecast",
        {"table": "s", "time_col": "ts", "measure": "value", "freq": "month", "horizon": 3},
        "forecast_summary",
        ("line", "time_bucket", ["yhat", "yhat_lower", "yhat_upper"]),
        "contract_forecast_conn",
    ),
]


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "fact_kind", "chart", "fixture"),
    SUCCESS_CASES,
    ids=[c[0] for c in SUCCESS_CASES],
)
def test_tool_succeeds_on_reference_data(
    request,
    contract_check,
    tool_name,
    kwargs,
    fact_kind,
    chart,
    fixture,
):
    connection = request.getfixturevalue(fixture) if fixture is not None else None
    result = execute_tool(tool_name, kwargs, connection=connection)
    contract_check.ok(result)
    contract_check.fact(result, fact_kind)
    if chart is not None:
        contract_check.chart(result, *chart)


# ---------- pydantic-ограничения: ValidationError ----------
# Literal, ge, min_length отклоняются до функции — это корректное
# поведение registry: параметры валидируются pydantic-моделью.

PARAMS_VALIDATION_CASES = [
    ("time_aggregate", {"table": "events", "time_col": "ts", "freq": "fortnight"}),
    ("segment_profile", {"table": "events", "group_by": []}),
    (
        "compare_segments",
        {"table": "events", "segment_col": "city", "a": "A", "b": "B", "measures": []},
    ),
    ("correlation", {"table": "events", "columns": ["amount"]}),
    ("correlation", {"table": "test", "columns": ["age", "id"], "min_n": 1}),
    ("correlation", {"table": "test", "columns": ["age", "id"], "method": "magic"}),
    (
        "lagged_correlation",
        {"table": "events", "time_col": "ts", "a": "amount", "b": "amount", "method": "magic"},
    ),
    ("forecast", {"table": "s", "time_col": "ts", "measure": "value", "horizon": 0}),
    (
        "forecast",
        {"table": "s", "time_col": "ts", "measure": "value", "horizon": 3, "method": "lstm"},
    ),
    ("forecast", {"table": "s", "time_col": "ts", "measure": "value", "horizon": 3, "alpha": 1.5}),
    ("forecast", {"table": "s", "time_col": "ts", "measure": "value", "horizon": 3, "holdout": 0}),
    ("find_duplicates", {"table": "events", "limit": 0}),
    ("detect_anomalies", {"table": "events", "measure": "amount", "limit": 0}),
]


@pytest.mark.parametrize(
    ("tool_name", "params"),
    PARAMS_VALIDATION_CASES,
    ids=[f"{c[0]}-pydantic-{i}" for i, c in enumerate(PARAMS_VALIDATION_CASES)],
)
def test_tool_rejects_invalid_params_via_pydantic(tool_name, params):
    with pytest.raises(ValidationError):
        execute_tool(tool_name, params)


# ---------- ошибки на уровне функции: ToolResult(status="error") ----------

FUNCTION_ERROR_CASES = [
    ("describe_column", {"table": "test", "column": "nonexistent"}, "invalid_column"),
    ("value_counts", {"table": "test", "column": "nonexistent"}, "invalid_column"),
    ("time_aggregate", {"table": "events", "time_col": "nonexistent"}, "invalid_column"),
    ("time_aggregate", {"table": "events", "time_col": "ts", "agg": "sum"}, "invalid_parameter"),
    ("find_duplicates", {"table": "events", "columns": ["nonexistent"]}, "invalid_column"),
    ("detect_anomalies", {"table": "events", "measure": "nonexistent"}, "invalid_column"),
    (
        "detect_anomalies",
        {"table": "events", "measure": "amount", "threshold": -1},
        "invalid_parameter",
    ),
    ("seasonality", {"table": "events", "time_col": "ts", "agg": "sum"}, "invalid_parameter"),
    ("correlation", {"table": "test", "columns": ["age", "nonexistent"]}, "invalid_column"),
    (
        "lagged_correlation",
        {"table": "events", "time_col": "ts", "a": "amount", "b": "nonexistent"},
        "invalid_column",
    ),
    (
        "forecast",
        {"table": "events", "time_col": "ts", "agg": "sum", "horizon": 3},
        "invalid_parameter",
    ),
]


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "expected_code"),
    FUNCTION_ERROR_CASES,
    ids=[f"{c[0]}-{c[2]}" for c in FUNCTION_ERROR_CASES],
)
def test_tool_returns_expected_error(contract_check, tool_name, kwargs, expected_code):
    result = execute_tool(tool_name, kwargs)
    contract_check.error(result, expected_code)


# ---------- compute_metric — отдельно из-за фикстуры metrics_dataset ----------


def test_compute_metric_contract(contract_check, metrics_dataset):
    connection, catalog = metrics_dataset
    result = execute_tool(
        "compute_metric",
        {"metric_id": "order_total"},
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
    )
    # без semantics — метрика не определена; это корректный отказ
    contract_check.error(result, "metric_undefined")


# ---------- покрытие реестра ----------


def test_every_registered_tool_has_a_contract_case():
    """Новый инструмент не появится в реестре без контрактной проверки."""
    registered = {t.name for t in TOOLS_REGISTRY}
    covered = (
        {c[0] for c in SUCCESS_CASES}
        | {c[0] for c in FUNCTION_ERROR_CASES}
        | {c[0] for c in PARAMS_VALIDATION_CASES}
    )
    covered.add("compute_metric")  # покрыт отдельным тестом
    missing = registered - covered
    assert not missing, (
        f"нет контрактных проверок для: {sorted(missing)}. "
        f"Добавьте case в SUCCESS_CASES / FUNCTION_ERROR_CASES / "
        f"PARAMS_VALIDATION_CASES или отдельный тест."
    )
