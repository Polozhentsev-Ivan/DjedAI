"""Прогноз с интервалами (B2-7): заложенные закономерности и отложенный период."""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import numpy as np
import pytest

from djedai.analytics.registry import TOOLS_REGISTRY, get_tools_for_llm
from djedai.analytics.tools import forecast


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


@pytest.fixture
def monthly_conn():
    """36 месяцев: тренд + сезонность 12."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = []
    for i in range(36):
        year = 2021 + i // 12
        month = i % 12 + 1
        v = 1000.0 + 50.0 * i + 100.0 * np.cos(2 * np.pi * (month - 7) / 12)
        rows.append((datetime(year, month, 1), float(v)))
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    return con


@pytest.fixture
def daily_conn():
    """180 дней: линейный тренд + дневная сезонность 7 + шум."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rng = np.random.default_rng(42)
    rows = []
    for i in range(180):
        v = 100.0 + 0.5 * i + 10.0 * np.cos(2 * np.pi * i / 7) + rng.normal(scale=1.0)
        rows.append((datetime(2024, 1, 1) + timedelta(days=i), float(v)))
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    return con


# ---------- горизонт и интервалы ----------


def test_forecast_returns_horizon_rows(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=6,
        measure="value",
        freq="month",
        connection=monthly_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 6
    assert result.data.columns == ["time_bucket", "yhat", "yhat_lower", "yhat_upper"]
    for row in result.data.rows:
        assert all(np.isfinite(v) for v in row[1:])


def test_forecast_intervals_contain_point(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=6,
        measure="value",
        freq="month",
        method="ets",
        connection=monthly_conn,
    )
    for row in result.data.rows:
        _, yhat, lower, upper = row
        assert lower <= yhat <= upper
        assert lower <= upper


def test_forecast_different_horizons(monthly_conn):
    for h in (1, 3, 12):
        result = forecast(
            "s",
            "ts",
            horizon=h,
            measure="value",
            freq="month",
            connection=monthly_conn,
        )
        assert result.status == "ok"
        assert result.data.row_count == h


def test_forecast_timestamps_follow_freq(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        connection=monthly_conn,
    )
    # последняя точка истории — 2023-12-01; прогноз — 2024-01-01, 02-01, 03-01
    assert [row[0] for row in result.data.rows] == [
        "2024-01-01",
        "2024-02-01",
        "2024-03-01",
    ]


# ---------- валидация на отложенном периоде ----------


def test_forecast_holdout_metrics(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        method="ets",
        holdout=3,
        connection=monthly_conn,
    )
    assert result.status == "ok"
    metrics = _by_kind(result, "holdout_metrics")
    assert len(metrics) == 1
    assert metrics[0].values["n_holdout"] == 3
    assert metrics[0].values["mae"] is not None
    assert metrics[0].values["rmse"] >= metrics[0].values["mae"]


def test_forecast_holdout_metrics_match_manual():
    """MAE на holdout сверяется с ручным расчётом на отложенных точках."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [(datetime(2024, 1, 1) + timedelta(days=i), 10.0 + i) for i in range(30)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="day",
        method="naive",
        period=1,
        holdout=3,
        connection=con,
    )
    assert result.status == "ok"
    metrics = _by_kind(result, "holdout_metrics")[0].values
    # naive(period=1) на train[0..26] предскажет последнее значение (36),
    # actual = [37, 38, 39] → MAE = (|36-37|+|36-38|+|36-39|)/3 = 2
    assert metrics["mae"] == pytest.approx(2.0, abs=1e-9)
    assert metrics["n_holdout"] == 3


def test_forecast_holdout_too_large_warns(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        holdout=100,
        connection=monthly_conn,
    )
    assert result.status == "ok"
    assert any("holdout" in f.text for f in _by_kind(result, "warning"))
    assert not _by_kind(result, "holdout_metrics")


def test_forecast_holdout_train_too_short_warns(monthly_conn):
    # min_required для month = 24; len=36; holdout=20 → train=16 < 24
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        holdout=20,
        connection=monthly_conn,
    )
    assert result.status == "ok"
    assert any("holdout" in f.text or "обучающая" in f.text for f in _by_kind(result, "warning"))
    assert not _by_kind(result, "holdout_metrics")


# ---------- короткий ряд и методы ----------


def test_forecast_short_series_errors():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    con.executemany(
        "INSERT INTO s VALUES (?, ?)",
        [(datetime(2024, 1, 1) + timedelta(days=i), float(i)) for i in range(3)],
    )
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="day",
        connection=con,
    )
    assert result.status == "error"
    assert result.error.code == "insufficient_data"
    assert "3" in result.error.message


def test_forecast_naive_method(daily_conn):
    result = forecast(
        "s",
        "ts",
        horizon=7,
        measure="value",
        freq="day",
        method="naive",
        period=7,
        connection=daily_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 7
    last_7 = [
        r[0] for r in daily_conn.execute("SELECT value FROM s ORDER BY ts DESC LIMIT 7").fetchall()
    ][::-1]
    for i, row in enumerate(result.data.rows):
        assert row[1] == pytest.approx(last_7[i])


def test_forecast_arima_method(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        method="arima",
        connection=monthly_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 3
    for row in result.data.rows:
        assert row[2] <= row[1] <= row[3]


def test_forecast_missing_daily_buckets_sum_is_zero_filled():
    """Для sum пропущенный день = 0, а не разрыв ряда."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = []
    for i in range(20):
        if i in (5, 10):  # «нет выгрузки» в эти дни
            continue
        rows.append((datetime(2024, 1, 1) + timedelta(days=i), 10.0))
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="day",
        method="naive",
        period=7,
        connection=con,
    )
    assert result.status == "ok", result.error
    # naive должен предсказать 0 на лагах, где в истории был пропуск,
    # а не сдвинуться на «соседнюю строку»
    assert result.data.row_count == 3


def test_forecast_missing_buckets_mean_is_error(monthly_conn):
    # Если в колонке-метрике есть NULL, для avg это станет missing_data.
    monthly_conn.execute("UPDATE s SET value = NULL WHERE ts = '2022-06-01'")
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        freq="month",
        agg="avg",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "missing_data"


# ---------- ошибки параметров ----------


def test_forecast_invalid_method(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        method="lstm",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_forecast_invalid_horizon(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=0,
        measure="value",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_forecast_invalid_alpha(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        alpha=1.5,
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_forecast_missing_measure_with_sum(monthly_conn):
    result = forecast("s", "ts", horizon=3, agg="sum", connection=monthly_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_forecast_missing_column(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="nope",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_forecast_non_numeric_measure(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="ts",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_forecast_invalid_filter(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        filter="1=1; DROP TABLE s",
        connection=monthly_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


def test_forecast_empty(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        filter="1=0",
        connection=monthly_conn,
    )
    assert result.status == "empty"


# ---------- формулировки и примеры ----------


def test_forecast_facts_have_no_causal_words(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        holdout=3,
        connection=monthly_conn,
    )
    forbidden = ("влияет", "вызывает", "приводит к", "из-за")
    for fact in result.facts:
        for word in forbidden:
            assert word not in fact.text, (word, fact.text)


def test_forecast_facts_have_caveat_and_endpoint(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        connection=monthly_conn,
    )
    assert _by_kind(result, "caveat")
    assert _by_kind(result, "forecast_summary")
    assert _by_kind(result, "forecast_endpoint")


def test_forecast_chart_hint(monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=3,
        measure="value",
        connection=monthly_conn,
    )
    assert result.chart_hint is not None
    assert result.chart_hint.type == "line"
    assert result.chart_hint.x == "time_bucket"
    assert "yhat" in result.chart_hint.y
    assert "yhat_lower" in result.chart_hint.y
    assert "yhat_upper" in result.chart_hint.y


def test_forecast_example_executes(monthly_conn):
    """Пример вызова из реестра работает на реальных данных."""
    definition = next(t for t in TOOLS_REGISTRY if t.name == "forecast")
    example = definition.parameters_model.model_json_schema()["examples"][0]
    params = {**example, "table": "s", "time_col": "ts", "measure": "value"}
    validated = definition.parameters_model.model_validate(params).model_dump(exclude_none=True)
    result = forecast(**validated, connection=monthly_conn)
    assert result.status == "ok", result.error
    assert result.data.row_count == params["horizon"]


def test_forecast_examples_pass_schema_validation():
    exported = next(t for t in get_tools_for_llm() if t["name"] == "forecast")
    definition = next(t for t in TOOLS_REGISTRY if t.name == "forecast")
    for example in exported["parameters_schema"]["examples"]:
        definition.parameters_model.model_validate(example)


@pytest.fixture
def long_monthly_conn():
    """60 месяцев: тренд + сезонность 12, достаточно для SARIMAX."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = []
    for i in range(60):
        year = 2019 + i // 12
        month = i % 12 + 1
        v = 1000.0 + 30.0 * i + 100.0 * np.cos(2 * np.pi * (month - 7) / 12)
        rows.append((datetime(year, month, 1), float(v)))
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)
    return con


def test_forecast_arima_seasonal_path(long_monthly_conn):
    result = forecast(
        "s",
        "ts",
        horizon=6,
        measure="value",
        freq="month",
        method="arima",
        connection=long_monthly_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 6
    for row in result.data.rows:
        assert row[2] <= row[1] <= row[3]


# ---------- дисперсия интервалов ----------


def test_forecast_ets_intervals_not_random_walk():
    """ETS на почти постоянном ряду: ширины не растут как sqrt(h).

    Для α≈0, β≈0 аналитическая ETS(A,A,N) даёт множитель ≈1 на любом h,
    а sqrt(h) на h=30 даёт завышение в ~5.5 раз.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rng = np.random.default_rng(42)
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=i), 100.0 + float(rng.normal())) for i in range(1000)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    result = forecast(
        "s",
        "ts",
        horizon=30,
        measure="value",
        freq="day",
        period=1,
        method="ets",
        connection=con,
    )
    assert result.status == "ok"
    last_row = result.data.rows[-1]
    half_width = (last_row[3] - last_row[2]) / 2
    # Аналитическая ETS ≈ z·σ ≈ 2; sqrt(30)·z·σ ≈ 10.5
    assert half_width < 4.0, f"half-width {half_width} растёт как sqrt(h) — регрессия"
    assert half_width > 0.5


def test_forecast_naive_seasonal_widths_equal_in_first_season():
    """Для period=7 h=1..7 множитель sqrt(1) — ширины одинаковы.

    Раньше использовался sqrt(h), и ширины росли: sqrt(1), sqrt(2), …, sqrt(7).
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rng = np.random.default_rng(0)
    base = datetime(2024, 1, 1)
    n = 200
    values = [float(rng.normal()) for _ in range(7)]
    for i in range(7, n):
        values.append(values[i - 7] + float(rng.normal()))
    rows = [(base + timedelta(days=i), values[i]) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    result = forecast(
        "s",
        "ts",
        horizon=14,
        measure="value",
        freq="day",
        period=7,
        method="naive",
        connection=con,
    )
    assert result.status == "ok"

    widths = [row[3] - row[2] for row in result.data.rows]
    first, second = widths[:7], widths[7:]

    # h=1..7: одинаковая ширина (sqrt(1))
    assert all(abs(w - first[0]) < 1e-9 for w in first), (
        f"ширины первого сезона должны совпадать, получено {first}"
    )
    # h=8..14: одинаковая ширина (sqrt(2))
    assert all(abs(w - second[0]) < 1e-9 for w in second)
    # второй сезон sqrt(2) шире первого
    assert second[0] == pytest.approx(first[0] * np.sqrt(2), rel=1e-6)


def test_forecast_naive_period_1_keeps_sqrt_h():
    """Для period=1 формула сводится к старой sqrt(h) — регрессии нет."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rng = np.random.default_rng(0)
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=i), 100.0 + float(rng.normal())) for i in range(100)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    result = forecast(
        "s",
        "ts",
        horizon=4,
        measure="value",
        freq="day",
        period=1,
        method="naive",
        connection=con,
    )
    assert result.status == "ok"
    widths = [row[3] - row[2] for row in result.data.rows]
    # множители sqrt(1..4) = 1, √2, √3, 2
    expected = [widths[0] * np.sqrt(h) for h in (1, 2, 3, 4)]
    for got, exp in zip(widths, expected, strict=False):
        assert got == pytest.approx(exp, rel=1e-6)


# ---------- отмена внутри fit ----------


def test_forecast_cancellation_before_dispatch(monkeypatch):
    """TurnCancelled на первом же check_budget — обучение не начинается."""
    from djedai.execution import TurnCancelled

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [(datetime(2024, 1, 1) + timedelta(days=i), 10.0 + i) for i in range(30)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    dispatched = {"n": 0}
    from djedai.analytics.tools import _dispatch_forecast as _orig

    def counting_dispatch(*args, **kwargs):
        dispatched["n"] += 1
        return _orig(*args, **kwargs)

    def cancel_immediately():
        raise TurnCancelled("cancel before dispatch")

    monkeypatch.setattr("djedai.analytics.tools.check_budget", cancel_immediately)
    monkeypatch.setattr("djedai.analytics.tools._dispatch_forecast", counting_dispatch)

    with pytest.raises(TurnCancelled):
        forecast("s", "ts", horizon=3, measure="value", freq="day", period=1, connection=con)

    assert dispatched["n"] == 0


def test_forecast_cancellation_inside_fit_propagates(monkeypatch):
    """TurnCancelled внутри _forecast_ets не превращается в forecast_failed."""
    from djedai.execution import TurnCancelled

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, value DOUBLE)")
    rows = [(datetime(2024, 1, 1) + timedelta(days=i), 10.0 + i) for i in range(30)]
    con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    calls = {"n": 0}

    def cancel_inside_fit():
        calls["n"] += 1
        # #1 — SQL; #2 — перед основным fit; #3 — внутри _forecast_ets
        if calls["n"] >= 3:
            raise TurnCancelled("cancel inside fit")

    monkeypatch.setattr("djedai.analytics.tools.check_budget", cancel_inside_fit)

    with pytest.raises(TurnCancelled):
        forecast(
            "s",
            "ts",
            horizon=3,
            measure="value",
            freq="day",
            period=1,
            method="ets",
            connection=con,
        )
