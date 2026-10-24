"""Degenerate STL components and SQL aliases have independent expectations."""

import json
import math
from datetime import datetime

import duckdb
import pytest

from djedai.analytics.tools import seasonality, segment_profile


def fact(result, kind):
    return next(f for f in result.facts if f.kind == kind)


@pytest.mark.parametrize("factor", [1.0, 1e-12, 1e12, -7.0])
@pytest.mark.parametrize("component", ["linear", "seasonal"])
def test_numerical_noise_does_not_create_a_missing_stl_component(factor, component):
    values = [
        factor
        * (1000 + (10 * i if component == "linear" else 100 * math.cos(2 * math.pi * i / 12)))
        for i in range(36)
    ]
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE series(ts TIMESTAMP, v DOUBLE)")
        con.executemany(
            "INSERT INTO series VALUES (?, ?)",
            [(datetime(2021 + i // 12, i % 12 + 1, 1), value) for i, value in enumerate(values)],
        )
        result = seasonality("series", "ts", measure="v", freq="month", connection=con)
    assert result.status == "ok", result.error
    summary = fact(result, "seasonal_summary").values
    assert summary["strength_seasonal" if component == "linear" else "strength_trend"] == 0
    assert summary["strength_trend" if component == "linear" else "strength_seasonal"] > 0.99
    if component == "linear":
        assert "слабая" in fact(result, "interpretation").text
    else:
        assert fact(result, "seasonal_peak").values["amplitude"] == pytest.approx(abs(factor) * 200)
    # Published decomposition remains in the original units and reconstructs the input.
    for row, value in zip(result.data.rows, values, strict=True):
        assert row[1] == value
        assert sum(row[2:]) == pytest.approx(value)
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


@pytest.mark.parametrize("column", ["segment_size", "SEGMENT_SIZE", "v_mean"])
@pytest.mark.parametrize("keys", [(10, 20), ("a", "b")])
def test_profile_source_columns_cannot_shadow_computed_statistics(column, keys):
    dtype = "INTEGER" if isinstance(keys[0], int) else "VARCHAR"
    with duckdb.connect(":memory:") as con:
        con.execute(f'CREATE TABLE groups("{column}" {dtype}, v DOUBLE)')
        con.executemany("INSERT INTO groups VALUES (?, ?)", [(keys[0], 1)] * 5 + [(keys[1], 99)])
        result = segment_profile("groups", [column], measures=["v"], top_n=1, connection=con)
    assert result.status == "ok", result.error
    assert result.data.rows[0][:3] == [keys[0], 5, 1]
    assert result.data.truncated is True
    assert fact(result, "segment_summary").values == {"segment_count": 2, "total_rows": 6}
    assert fact(result, "segment_measure").values["max_mean"] == 99
    assert fact(result, "segment_measure").values["min_mean"] == 1
    assert len({name.casefold() for name in result.data.columns}) == len(result.data.columns)
    assert result.chart_hint.x == column
    assert result.chart_hint.y == [result.data.columns[2]]


def test_count_only_profile_avoids_existing_suffixed_names():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE groups(segment_size VARCHAR, segment_size_1 VARCHAR)")
        con.execute("INSERT INTO groups VALUES ('a', 'x'), ('a', 'x'), ('b', 'y')")
        result = segment_profile("groups", ["segment_size", "segment_size_1"], connection=con)
    assert result.status == "ok", result.error
    assert result.data.columns == ["segment_size", "segment_size_1", "segment_size_2"]
    assert result.data.rows == [["a", "x", 2], ["b", "y", 1]]
    assert fact(result, "segment_summary").values == {"segment_count": 2, "total_rows": 3}
