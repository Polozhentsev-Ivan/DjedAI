"""Корреляции и лаги (B2-6): эталоны и заложенный лаг 5 дней."""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import numpy as np
import pytest
from scipy import stats

from djedai.analytics.registry import TOOLS_REGISTRY, get_tools_for_llm
from djedai.analytics.tools import correlation, lagged_correlation


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


# ---------- корреляция ----------


@pytest.fixture
def corr_conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (x DOUBLE, y DOUBLE, z DOUBLE, label VARCHAR)")
    rng = np.random.default_rng(0)
    x = rng.normal(size=200)
    y = 2.0 * x + rng.normal(scale=0.1, size=200)
    z = rng.normal(size=200)  # независимый шум
    rows = [(float(a), float(b), float(c), "row") for a, b, c in zip(x, y, z, strict=False)]
    con.executemany("INSERT INTO m VALUES (?, ?, ?, ?)", rows)
    return con


def test_correlation_matches_scipy(corr_conn):
    result = correlation("m", ["x", "y"], method="pearson", connection=corr_conn)
    assert result.status == "ok"
    row = next(r for r in result.data.rows if r[0] == "x" and r[1] == "y")
    _, _, r, p, n = row
    expected_r, expected_p = stats.pearsonr(
        [v[0] for v in corr_conn.execute("SELECT x, y FROM m").fetchall()],
        [v[1] for v in corr_conn.execute("SELECT x, y FROM m").fetchall()],
    )
    assert r == pytest.approx(expected_r, abs=1e-9)
    assert p == pytest.approx(expected_p, abs=1e-9)
    assert n == 200


def test_correlation_reports_strength_and_direction(corr_conn):
    result = correlation("m", ["x", "y"], connection=corr_conn)
    metric = next(f for f in _by_kind(result, "metric") if f.values["a"] == "x")
    assert metric.values["r"] > 0.9
    assert "положительная" in metric.text and "сильная" in metric.text
    assert _by_kind(result, "caveat")


def test_correlation_no_link(corr_conn):
    result = correlation("m", ["x", "z"], connection=corr_conn)
    metric = next(f for f in _by_kind(result, "metric") if f.values["b"] == "z")
    assert abs(metric.values["r"]) < 0.3


def test_correlation_invalid_column(corr_conn):
    result = correlation("m", ["x", "nope"], connection=corr_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_correlation_invalid_method(corr_conn):
    result = correlation("m", ["x", "y"], method="magic", connection=corr_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_correlation_needs_two_columns(corr_conn):
    result = correlation("m", ["x"], connection=corr_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_correlation_non_numeric_column(corr_conn):
    result = correlation("m", ["x", "label"], connection=corr_conn)
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_correlation_empty_result(corr_conn):
    result = correlation("m", ["x", "y"], filter="1=0", connection=corr_conn)
    assert result.status == "empty"
    assert _by_kind(result, "empty_result")


# ---------- лаги ----------


@pytest.fixture
def lagged_conn():
    """a(t) и b(t) = a(t-5) + шум: a опережает b на 5 дней."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(42)
    n = 180
    a = rng.normal(size=n).cumsum()
    b = np.concatenate([np.zeros(5), a[:-5]]) + rng.normal(scale=0.1, size=n)
    start = datetime(2024, 1, 1)
    rows = [(start + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)
    return con


def test_lagged_finds_planted_5_days(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=10,
        connection=lagged_conn,
    )
    assert result.status == "ok"
    metric = _by_kind(result, "metric")[0]
    assert metric.values["lag"] == 5
    assert abs(metric.values["r"]) > 0.9
    assert "опережают" in metric.text
    assert _by_kind(result, "caveat")


def test_lagged_negative_lag_when_b_leads(lagged_conn):
    """a(t) = b(t-5) + шум: b опережает a на 5 дней → lag = -5."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(7)
    n = 180
    b = rng.normal(size=n).cumsum()
    a = np.concatenate([np.zeros(5), b[:-5]]) + rng.normal(scale=0.1, size=n)
    start = datetime(2024, 1, 1)
    rows = [(start + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=10,
        connection=con,
    )
    assert result.status == "ok"
    assert _by_kind(result, "metric")[0].values["lag"] == -5


def test_lagged_no_link(lagged_conn):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(1)
    n = 180
    a = rng.normal(size=n)
    b = rng.normal(size=n)
    start = datetime(2024, 1, 1)
    rows = [(start + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=10,
        connection=con,
    )
    assert result.status == "ok"
    best = _by_kind(result, "metric")[0].values
    assert abs(best["r"]) < 0.5
    assert "сильная" not in _by_kind(result, "metric")[0].text


def test_lagged_invalid_freq(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="fortnight",
        connection=lagged_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_lagged_invalid_column(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "nope",
        connection=lagged_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_lagged_non_numeric_column(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "ts",
        connection=lagged_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_lagged_negative_max_lag(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        max_lag=-1,
        connection=lagged_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_lagged_empty(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        filter="1=0",
        connection=lagged_conn,
    )
    assert result.status == "empty"
    assert _by_kind(result, "empty_result")


def test_lagged_granger_is_optional_and_warns(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        include_granger=True,
        connection=lagged_conn,
    )
    warnings = [f for f in result.facts if f.kind == "warning"]
    assert not warnings, f"Granger упал: {warnings[0].text}"
    assert result.status == "ok"

    granger = _by_kind(result, "granger")
    assert len(granger) == 1
    text = granger[0].text

    assert "предсказуемость" in text
    assert "причинность" in text
    assert "не причинность" in text
    for word in ("влияет", "вызывает", "приводит к", "из-за", "определяет"):
        assert word not in text, (word, text)


def test_facts_have_no_causal_words(corr_conn, lagged_conn):
    forbidden = ("влияет", "вызывает", "приводит к", "из-за")
    for result in (
        correlation("m", ["x", "y"], connection=corr_conn),
        lagged_correlation(
            "s",
            "ts",
            "a",
            "b",
            include_granger=True,
            connection=lagged_conn,
        ),
    ):
        for fact in result.facts:
            for word in forbidden:
                assert word not in fact.text, (word, fact.text)


def test_facts_match_data_rows(lagged_conn):
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=10,
        connection=lagged_conn,
    )
    metric = next(f for f in result.facts if f.kind == "metric")
    k = metric.values["lag"]
    row = next(r for r in result.data.rows if r[0] == k)
    assert metric.values["r"] == pytest.approx(row[1])
    assert metric.values["n"] == row[3]


def test_correlation_facts_match_data(corr_conn):
    result = correlation("m", ["x", "y", "z"], connection=corr_conn)
    by_pair = {(r[0], r[1]): r for r in result.data.rows}
    for fact in (f for f in result.facts if f.kind == "metric"):
        a_, b_ = fact.values["a"], fact.values["b"]
        _, _, r, p, n = by_pair[(a_, b_)]
        assert fact.values["r"] == pytest.approx(r)
        assert fact.values["p"] == pytest.approx(p)
        assert fact.values["n"] == n


def test_agent_call_examples_are_executable():
    """Примеры из реестра не просто валидны, но и выполняются на мок-данных."""
    from djedai.analytics.tools import _get_mock_connection

    con = _get_mock_connection()
    try:
        for tool_name, fn in (
            ("correlation", correlation),
            ("lagged_correlation", lagged_correlation),
        ):
            definition = next(t for t in TOOLS_REGISTRY if t.name == tool_name)
            schema = definition.parameters_model.model_json_schema()
            examples = schema.get("examples", [])
            assert examples, f"{tool_name}: нет examples в модели параметров"
            for example in examples:
                params = definition.parameters_model.model_validate(example).model_dump(
                    exclude_none=True
                )
                result = fn(**params, connection=con)
                assert result.status in {"ok", "empty"}, (example, result.error)
    finally:
        con.close()


def test_registry_snapshot_matches():
    """Снапшот EXPECTED_TOOLS совпадает с реальным содержимым реестра."""
    from test_analytics_registry import EXPECTED_TOOLS

    assert get_tools_for_llm() == EXPECTED_TOOLS


def test_lagged_aligns_by_calendar_not_row():
    """Пропуски календаря не должны менять единицы лага."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(0)
    n = 100
    a = rng.normal(size=n)
    b = np.concatenate([np.zeros(5), a[:-5]])
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=2 * i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)

    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=12,
        connection=con,
    )
    assert result.status == "ok"
    metric = _by_kind(result, "metric")[0]
    # с reindex сетка — каждый день, сдвиг b[i] = a[i-5] на позициях
    # соответствует 10 календарным дням
    assert metric.values["lag"] == 10
    assert abs(metric.values["r"]) > 0.9


def test_correlation_uses_full_population_not_first_5000():
    """SQL_MAX_ROWS не должен подменять вход статистики."""
    from scipy import stats

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE big AS "
        "SELECT i::DOUBLE x, "
        "CASE WHEN i < 5000 THEN i::DOUBLE ELSE -i::DOUBLE END y "
        "FROM range(10000) t(i)"
    )
    result = correlation("big", ["x", "y"], connection=con)
    assert result.status == "ok"

    full = con.execute("SELECT x, y FROM big").fetchall()
    expected_r, expected_p = stats.pearsonr([r[0] for r in full], [r[1] for r in full])

    row = next(r for r in result.data.rows if r[0] == "x" and r[1] == "y")
    assert row[2] == pytest.approx(expected_r, abs=1e-9)
    assert row[4] == 10000
    # контроль: направление действительно отрицательное
    assert row[2] < 0
    assert any("отрицательная" in f.text for f in _by_kind(result, "metric"))


@pytest.mark.parametrize("tool_name", ["correlation", "lagged_correlation"])
def test_cancellation_stops_cpu_work_early(monkeypatch, tool_name):
    """Отмена во время CPU-работы останавливает цикл, а не ждёт конца.

    Раньше check_budget не вызывался внутри NumPy/SciPy-циклов, и после отмены
    успевали выполниться все пары/лаги. Тест подменяет check_budget: второй вызов
    бросает TurnCancelled и проверяет, что дальше цикл не идёт.
    """
    from djedai.execution import TurnCancelled

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (ts TIMESTAMP, a DOUBLE, b DOUBLE, c DOUBLE, d DOUBLE)")
    rng = np.random.default_rng(0)
    base = datetime(2024, 1, 1)
    rows = [
        (
            base + timedelta(days=i),
            float(rng.normal()),
            float(rng.normal()),
            float(rng.normal()),
            float(rng.normal()),
        )
        for i in range(50)
    ]
    con.executemany("INSERT INTO m VALUES (?, ?, ?, ?, ?)", rows)

    calls = {"n": 0}

    def cancelling_check():
        calls["n"] += 1
        if calls["n"] > 1:
            raise TurnCancelled("cancelled after first step")

    monkeypatch.setattr("djedai.analytics.tools.check_budget", cancelling_check)

    with pytest.raises(TurnCancelled):
        if tool_name == "correlation":
            # 4 колонки → 6 пар, но после первой отмены цикл должен прерваться
            correlation("m", ["a", "b", "c", "d"], connection=con)
        else:
            # 3 лага (−1, 0, +1); после первого — отмена
            lagged_correlation(
                "m",
                "ts",
                "a",
                "b",
                freq="day",
                max_lag=1,
                include_granger=True,
                connection=con,
            )

    # ровно два вызова: первый пропустил одну итерацию, второй бросил
    assert calls["n"] == 2


def test_lagged_calendar_grid_is_bounded():
    """Одна ошибочная дата не должна вызывать гигабайты аллокации.

    Тимлид: 12 событий, одно с ts=1970-01-01, остальные 26.10.2026,
    freq='minute' → ~30M бакетов. Ожидается понятная ошибка, а не OOM.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rows = [(datetime(1970, 1, 1), 1.0, 2.0)]
    rows += [(datetime(2026, 10, 26, 12, i), float(i), float(i + 1)) for i in range(11)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)

    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="minute",
        max_lag=2,
        connection=con,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
    assert "бакет" in result.error.message
    assert result.error.suggestion is not None
    assert any(word in result.error.suggestion for word in ("Укрупните", "сузьте", "проверьте"))


def test_lagged_calendar_grid_within_limit_still_works():
    """Обычный ряд (1000 дней) проходит защиту и даёт корреляцию."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(0)
    n = 1000
    a = rng.normal(size=n)
    b = np.concatenate([np.zeros(5), a[:-5]])
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)

    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        max_lag=10,
        connection=con,
    )
    assert result.status == "ok"
    metric = _by_kind(result, "metric")[0]
    assert metric.values["lag"] == 5


def test_lagged_calendar_grid_message_suggests_coarser_freq():
    """Сообщение об ошибке предлагает конкретное действие."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rows = [(datetime(1970, 1, 1), 1.0, 2.0)] + [
        (datetime(2026, 10, 26, 12, i), float(i), float(i + 1)) for i in range(11)
    ]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)
    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="minute",
        connection=con,
    )
    # совет предлагает именно укрупнить частоту — это то, что реально поможет
    assert "day" in result.error.suggestion or "hour" in result.error.suggestion


def test_estimate_calendar_buckets_upper_bound():
    """Оценка сверху: не меньше фактического размера для реальных частот."""
    from djedai.analytics.tools import _estimate_calendar_buckets

    start = datetime(2024, 1, 1)
    end = datetime(2024, 12, 31)
    # day: 366 дней (2024 — високосный)
    assert _estimate_calendar_buckets(start, end, "day") >= 366
    # month: ~12 месяцев
    assert 12 <= _estimate_calendar_buckets(start, end, "month") <= 14
    # minute: заведомо больше, чем day
    assert _estimate_calendar_buckets(start, end, "minute") > 500_000


def test_granger_refuses_irregular_grid():
    """Granger не сжимает календарные пропуски."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(0)
    n = 70
    a = rng.normal(size=n).cumsum()
    b = np.concatenate([np.zeros(1), a[:-1]])
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=2 * i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)

    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="avg",
        max_lag=5,
        include_granger=True,
        connection=con,
    )

    # основная кривая корректна: с reindex лаг 2 дня (b отстаёт на 1 наблюдение = 2 дня)
    assert result.status == "ok"
    metric = _by_kind(result, "metric")[0]
    assert metric.values["lag"] == 2

    # Granger отказался — warning, без kind="granger"
    assert not _by_kind(result, "granger")
    warnings = _by_kind(result, "warning")
    assert any("Granger" in w.text and "регулярн" in w.text for w in warnings)


def test_granger_runs_on_continuous_grid_with_sum():
    """Для agg='sum' пропуски заполняются нулями — Granger работает на полной сетке."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE s (ts TIMESTAMP, a DOUBLE, b DOUBLE)")
    rng = np.random.default_rng(0)
    n = 70
    a = rng.normal(size=n)
    # шум обязателен: детерминированная связь даёт InfeasibleTestError
    # (VAR подгоняется идеально, F-статистика не считается)
    b = np.concatenate([np.zeros(1), a[:-1]]) + rng.normal(scale=0.1, size=n)
    base = datetime(2024, 1, 1)
    rows = [(base + timedelta(days=i), float(a[i]), float(b[i])) for i in range(n)]
    con.executemany("INSERT INTO s VALUES (?, ?, ?)", rows)

    result = lagged_correlation(
        "s",
        "ts",
        "a",
        "b",
        freq="day",
        agg="sum",
        max_lag=2,
        include_granger=True,
        connection=con,
    )
    assert result.status == "ok"

    # никаких warning — Granger должен посчитаться
    warnings = [f for f in result.facts if f.kind == "warning"]
    assert not warnings, f"Granger не выполнился: {warnings[0].text}"
    assert _by_kind(result, "granger")
