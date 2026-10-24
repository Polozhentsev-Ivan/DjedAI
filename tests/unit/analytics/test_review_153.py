"""Independent numerical and calendar expectations from review #153."""

import json
import math
from datetime import datetime, timedelta

import duckdb
import pytest

from djedai.analytics.tools import compare_segments, seasonality, segment_profile


def fact(result, kind):
    return next(f for f in result.facts if f.kind == kind)


def monthly(con, start_month=1, missing=False):
    con.execute("CREATE TABLE series(ts TIMESTAMP, v DOUBLE)")
    rows = []
    for i in range(36):
        month = start_month - 1 + i
        value = 100 + 100 * math.cos(2 * math.pi * ((month % 12 + 1) - 7) / 12)
        rows.append(
            (
                datetime(2021 + month // 12, month % 12 + 1, 1),
                None if missing and i == 15 else value,
            )
        )
    con.executemany("INSERT INTO series VALUES (?, ?)", rows)


def test_weekly_index_preserves_monday_observations():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE series(ts TIMESTAMP, v DOUBLE)")
        con.executemany(
            "INSERT INTO series VALUES (?, ?)",
            [
                (
                    datetime(2023, 1, 2) + timedelta(weeks=i),
                    100 + 50 * math.cos(2 * math.pi * i / 4),
                )
                for i in range(16)
            ],
        )
        result = seasonality("series", "ts", measure="v", freq="week", period=4, connection=con)
    assert result.status == "ok", result.error
    assert fact(result, "seasonal_summary").values["strength_seasonal"] > 0.99
    assert [row[1] for row in result.data.rows][:4] == pytest.approx([150, 100, 50, 100])


def test_existing_null_bucket_is_an_explicit_missing_data_error():
    with duckdb.connect(":memory:") as con:
        monthly(con, missing=True)
        result = seasonality("series", "ts", measure="v", freq="month", connection=con)
    assert result.status == "error"
    assert result.error.code == "missing_data"
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_month_label_uses_calendar_start_not_relative_phase():
    with duckdb.connect(":memory:") as con:
        monthly(con, start_month=4)
        result = seasonality("series", "ts", measure="v", freq="month", connection=con)
    assert result.status == "ok", result.error
    assert fact(result, "seasonal_peak").values["peak_phase"] == 3
    assert "июль" in fact(result, "seasonal_peak").text


def test_hourly_labels_and_output_limit_preserve_full_series_statistics(monkeypatch):
    monkeypatch.setenv("SQL_MAX_ROWS", "3")
    from djedai.config import get_limits

    get_limits.cache_clear()
    try:
        with duckdb.connect(":memory:") as con:
            con.execute("CREATE TABLE series(ts TIMESTAMP, v DOUBLE)")
            con.executemany(
                "INSERT INTO series VALUES (?, ?)",
                [
                    (
                        datetime(2026, 1, 1) + timedelta(hours=i),
                        100 + 25 * math.sin(2 * math.pi * i / 24),
                    )
                    for i in range(48)
                ],
            )
            result = seasonality(
                "series", "ts", measure="v", freq="hour", period=24, connection=con
            )
        assert result.status == "ok", result.error
        assert result.data.row_count == 3
        assert len(result.data.rows) == 3
        assert result.data.truncated is True
        assert [row[0] for row in result.data.rows] == [
            "2026-01-01T00:00:00",
            "2026-01-01T01:00:00",
            "2026-01-01T02:00:00",
        ]
        assert fact(result, "seasonal_peak").values["amplitude"] == pytest.approx(50, rel=0.01)
    finally:
        get_limits.cache_clear()


def test_numeric_segment_codes_are_resolved_by_sql_type():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE groups(seg INTEGER, v DOUBLE)")
        con.executemany(
            "INSERT INTO groups VALUES (?, ?)", [(1 + i // 20, float(i)) for i in range(40)]
        )
        result = compare_segments("groups", "seg", "1", "2", measures=["v"], connection=con)
    assert result.status == "ok", result.error
    assert result.data.rows[0][2:6] == pytest.approx([20, 20, 9.5, 29.5])


def test_different_constants_do_not_claim_a_negligible_effect():
    with duckdb.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE groups AS SELECT CASE WHEN i < 20 THEN 'A' ELSE 'B' END seg, "
            "CASE WHEN i < 20 THEN 1.0 ELSE 2.0 END v FROM range(40) t(i)"
        )
        result = compare_segments("groups", "seg", "A", "B", measures=["v"], connection=con)
    assert result.status == "ok", result.error
    assert result.data.rows[0][6] is None
    assert result.data.rows[0][-1] == "undefined_effect"
    assert "negligible" not in fact(result, "compare_numeric").text
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_null_and_literal_none_are_distinct_contingency_categories():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE groups(seg VARCHAR, category VARCHAR)")
        rows = [("A", None)] * 20 + [("A", "None")] * 10 + [("A", "x")] * 10
        rows += [("B", None)] * 10 + [("B", "None")] * 20 + [("B", "x")] * 10
        con.executemany("INSERT INTO groups VALUES (?, ?)", rows)
        result = compare_segments("groups", "seg", "A", "B", measures=["category"], connection=con)
    assert result.status == "ok", result.error
    row = result.data.rows[0]
    assert row[2:4] == [40, 40]
    assert row[7] == pytest.approx(0.035673993347252395)
    assert row[8] is True


def test_group_sizes_and_numeric_statistics_apply_the_same_filter():
    with duckdb.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE groups AS SELECT CASE WHEN i < 20 THEN 'A' ELSE 'B' END seg, "
            "i::DOUBLE v, i % 2 = 0 keep FROM range(40) t(i)"
        )
        result = compare_segments(
            "groups", "seg", "A", "B", measures=["v"], filter="keep", connection=con
        )
    assert result.status == "ok", result.error
    assert result.data.rows[0][2:4] == [10, 10]
    assert fact(result, "compare_summary").values["n_a"] == 10
    assert fact(result, "compare_summary").values["n_b"] == 10


def test_profile_global_totals_and_extremes_do_not_depend_on_top_n():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE groups(seg VARCHAR, v DOUBLE)")
        con.executemany(
            "INSERT INTO groups VALUES (?, ?)", [("a", 1)] * 5 + [("b", 99)] * 2 + [("c", -99)]
        )
        result = segment_profile("groups", ["seg"], measures=["v"], top_n=1, connection=con)
    assert result.status == "ok", result.error
    assert result.data.rows[0][:2] == ["a", 5]
    assert result.data.truncated is True
    assert fact(result, "segment_summary").values == {"segment_count": 3, "total_rows": 8}
    assert fact(result, "segment_measure").values["max_mean"] == 99
    assert fact(result, "segment_measure").values["min_mean"] == -99
