"""Review regressions for #152: real scoped dispatch and affine-invariant STL."""

import math
from datetime import datetime, timedelta

import duckdb
import pytest

from djedai.analytics.registry import execute_tool
from djedai.analytics.tools import detect_anomalies, find_duplicates, time_aggregate

TOOL_CASES = [
    ("time_aggregate", {"time_col": "ts", "measure": "v", "agg": "sum"}),
    ("find_duplicates", {"columns": ["g", "v"]}),
    ("detect_anomalies", {"measure": "v", "method": "iqr"}),
    ("detect_anomalies", {"measure": "v", "method": "stl-residual", "time_col": "ts"}),
]


@pytest.fixture
def connection():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE metrics(ts TIMESTAMP, g INTEGER, v DOUBLE)")
        con.executemany(
            "INSERT INTO metrics VALUES (?, 7, ?)",
            [(datetime(2026, 1, 1) + timedelta(days=i), 10 + i % 3) for i in range(84)],
        )
        con.execute("CREATE TABLE raw_private(v DOUBLE)")
        con.execute("INSERT INTO raw_private VALUES (999999)")
        yield con


@pytest.mark.parametrize("tool,params", TOOL_CASES)
def test_registered_tools_accept_the_real_dataset_context(connection, tool, params):
    result = execute_tool(
        tool, {"table": "metrics", **params}, connection=connection, allowed_tables={"metrics"}
    )
    assert result.status in {"ok", "empty"}, result.error
    result.model_dump(mode="json")


@pytest.mark.parametrize("tool,params", TOOL_CASES)
@pytest.mark.parametrize(
    "filter_sql",
    [
        "v < (SELECT max(v) FROM raw_private)",
        "EXISTS (SELECT 1 FROM information_schema.tables)",
        "EXISTS (SELECT 1 FROM read_csv('/private.csv'))",
    ],
)
def test_filter_subqueries_cannot_escape_the_selected_dataset(connection, tool, params, filter_sql):
    result = execute_tool(
        tool,
        {"table": "metrics", "filter": filter_sql, **params},
        connection=connection,
        allowed_tables={"metrics"},
    )
    assert result.status == "error"
    assert result.error.code == "dataset_scope_violation"
    assert "999999" not in result.model_dump_json()


@pytest.mark.parametrize(
    "function,params",
    [
        (time_aggregate, {"time_col": "ts"}),
        (find_duplicates, {}),
        (detect_anomalies, {"measure": "v"}),
    ],
)
def test_direct_functions_reject_foreign_table_before_reading_metadata(
    connection, function, params
):
    result = function("raw_private", **params, connection=connection, allowed_tables={"metrics"})
    assert result.status == "error"
    assert result.error.code == "dataset_scope_violation"


@pytest.mark.parametrize("factor,offset", [(1, 0), (1e-12, 0), (1, 1e12), (-7, 0)])
def test_stl_retains_spike_dates_after_changing_units_or_level(factor, offset):
    spikes = {20, 40, 60}
    start = datetime(2026, 1, 1)
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE metrics(ts TIMESTAMP, v DOUBLE)")
        con.executemany(
            "INSERT INTO metrics VALUES (?, ?)",
            [
                (
                    start + timedelta(days=i),
                    offset
                    + factor
                    * (
                        100
                        + 5 * math.sin(2 * math.pi * i / 7)
                        + 0.2 * math.cos(i)
                        + (50 if i in spikes else 0)
                    ),
                )
                for i in range(84)
            ],
        )
        result = detect_anomalies(
            "metrics", "v", method="stl-residual", time_col="ts", connection=con
        )
    assert result.status == "ok", result.error
    assert {row[0][:10] for row in result.data.rows} == {
        (start + timedelta(days=i)).date().isoformat() for i in spikes
    }
    result.model_dump(mode="json")


@pytest.mark.parametrize("value", [0.0, 100.0, 1e-12, 1e12])
def test_stl_never_turns_constant_roundoff_into_anomalies(value):
    with duckdb.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE metrics AS SELECT TIMESTAMP '2026-01-01' "
            "+ i * INTERVAL '1 day' ts, ?::DOUBLE v FROM range(84) t(i)",
            [value],
        )
        result = detect_anomalies(
            "metrics", "v", method="stl-residual", time_col="ts", connection=con
        )
    assert result.status == "empty", result.error
    assert result.data.rows == []
