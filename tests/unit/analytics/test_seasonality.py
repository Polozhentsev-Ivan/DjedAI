import math

import duckdb
import pytest

from djedai.analytics.tools import seasonality


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


@pytest.fixture
def seasonal_conn():
    """36 месяцев: чистая синусоида с пиком в июле, без тренда."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = []
    for i in range(36):
        year = 2021 + i // 12
        month = i % 12 + 1
        value = 1000.0 + 100.0 * math.cos(2 * math.pi * (month - 7) / 12)
        rows.append((f"{year}-{month:02d}-01 00:00:00", value))
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    return con


def test_strength_and_peak_detected(seasonal_conn):
    result = seasonality(
        "s",
        "ts",
        measure="value",
        agg="sum",
        freq="month",
        period=12,
        connection=seasonal_conn,
    )
    assert result.status == "ok"
    summary = _by_kind(result, "seasonal_summary")[0].values
    assert summary["strength_seasonal"] > 0.9
    peak = _by_kind(result, "seasonal_peak")[0].values
    # Пик — июль (phase 6, 0-based); допускаем ±1 из-за STL-сглаживания
    assert peak["peak_phase"] in {5, 6, 7}
    assert peak["amplitude"] > 100.0


def test_interpretation_present(seasonal_conn):
    result = seasonality(
        "s",
        "ts",
        measure="value",
        freq="month",
        connection=seasonal_conn,
    )
    interp = _by_kind(result, "interpretation")[0]
    assert "сильная" in interp.text


def test_chart_hint_is_line_connection(seasonal_conn):
    result = seasonality(
        "s",
        "ts",
        measure="value",
        freq="month",
        connection=seasonal_conn,
    )
    assert result.chart_hint is not None
    assert result.chart_hint.type == "line"
    assert "value" in result.chart_hint.y
    assert "trend" in result.chart_hint.y


def test_insufficient_data_returns_error():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    con.executemany(
        "INSERT INTO s VALUES (?, ?)",
        [(f"2024-{i:02d}-01 00:00:00", float(i)) for i in range(1, 7)],
    )
    result = seasonality(
        "s",
        "ts",
        measure="value",
        freq="month",
        period=12,
        connection=con,
    )
    assert result.status == "error"
    assert result.error.code == "insufficient_data"


def test_constant_series_returns_empty():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [
        (f"{year}-{month:02d}-01 00:00:00", 5.0) for year in (2023, 2024) for month in range(1, 13)
    ]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    result = seasonality(
        "s",
        "ts",
        measure="value",
        freq="month",
        period=12,
        connection=con,
    )
    assert result.status == "empty"
    assert _by_kind(result, "empty_result")


def test_invalid_freq_returns_error_connection(seasonal_conn):
    result = seasonality("s", "ts", measure="value", freq="week", connection=seasonal_conn)
    # week — валидная частота, но period по умолчанию = 52 → данных мало
    assert result.status in {"error", "empty"}

    result = seasonality("s", "ts", measure="value", freq="fortnight", connection=seasonal_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_sum_without_measure_returns_error_connection(seasonal_conn):
    result = seasonality("s", "ts", agg="sum", freq="month", connection=seasonal_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_invalid_period_returns_error(seasonal_conn):
    result = seasonality(
        "s",
        "ts",
        measure="value",
        freq="month",
        period=1,
        connection=seasonal_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
