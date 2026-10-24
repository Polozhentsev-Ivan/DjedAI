"""A missing STL bucket must not be reported as an absence of anomalies."""

import json
import math
from datetime import datetime, timedelta

import duckdb
import pytest

from djedai.analytics.registry import execute_tool


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("unknown,code", [(None, "missing_data"), (float("inf"), "invalid_data")])
def test_stl_unknown_bucket_is_an_explicit_error(grouped, unknown, code):
    start = datetime(2026, 1, 1)
    spikes = {20, 40, 60}
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE metrics(ts TIMESTAMP, g INTEGER, v DOUBLE)")
        con.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?)",
            [
                (
                    start + timedelta(days=i),
                    group,
                    100
                    + 5 * math.sin(2 * math.pi * i / 7)
                    + 0.2 * math.cos(i)
                    + (50 if i in spikes else 0),
                )
                for group in ([7, 8] if grouped else [7])
                for i in range(84)
            ],
        )
        params = {
            "table": "metrics",
            "measure": "v",
            "method": "stl-residual",
            "time_col": "ts",
            "group_by": ["g"] if grouped else [],
        }
        baseline = execute_tool(
            "detect_anomalies", params, connection=con, allowed_tables={"metrics"}
        )
        assert baseline.status == "ok", baseline.error
        assert len(baseline.data.rows) == (6 if grouped else 3)
        con.execute(
            "UPDATE metrics SET v = ? WHERE g = 7 AND ts = ?", [unknown, start + timedelta(days=10)]
        )
        result = execute_tool(
            "detect_anomalies", params, connection=con, allowed_tables={"metrics"}
        )
    assert result.status == "error"
    assert result.error.code == code
    assert not result.facts
    assert result.data is None
    json.dumps(result.model_dump(mode="json"), allow_nan=False)
