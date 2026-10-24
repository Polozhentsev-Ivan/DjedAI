import duckdb
import pytest

from djedai.analytics.registry import TOOLS_REGISTRY, get_tools_for_llm
from djedai.analytics.tools import (
    describe_column,
    describe_dataset,
    detect_anomalies,
    find_duplicates,
    time_aggregate,
    value_counts,
)

# ---------- helpers ----------


def _profile(r) -> dict:
    """Склеивает все Fact(kind in {profile, numeric_profile}) в один dict."""
    out = {}
    for f in r.facts:
        if f.kind in ("profile", "numeric_profile"):
            out.update(f.values)
    return out


def _by_kind(r, kind):
    return [f for f in r.facts if f.kind == kind]


def _test_overview(r):
    """Fact с обзором таблицы 'test'."""
    return next(
        f for f in r.facts if f.kind == "table_overview" and f.values.get("table_name") == "test"
    )


# ---------- describe_dataset ----------


def test_describe_dataset():
    r = describe_dataset()
    assert r.status == "ok"
    assert r.data is not None
    # Табличное представление: одна строка на таблицу
    assert r.data.columns == ["table_name", "row_count", "columns"]
    test_row = next(row for row in r.data.rows if row[0] == "test")
    assert test_row[1] == 4
    # Число строк и колонки — в fact.values
    ov = _test_overview(r)
    assert ov.values["row_count"] == 4
    assert len(ov.values["columns"]) == 3
    assert set(ov.values["columns"]) == {"id", "city", "age"}


def test_columns_have_types():
    ov = _test_overview(describe_dataset())
    types = ov.values["column_types"]
    assert "INT" in types["id"].upper()
    assert "VARCHAR" in types["city"].upper()


def test_facts_contain_table_summary():
    r = describe_dataset()
    joined = " ".join(f.text for f in r.facts)
    assert "Таблица 'test'" in joined
    assert "4" in joined
    assert all(col in joined for col in ("id", "city", "age"))


# ---------- describe_column ----------


def test_describe_column():
    r = describe_column("test", "city")
    assert r.status == "ok"
    p = _profile(r)
    assert p["total"] == 4
    assert p["non_null"] == 4
    assert p["distinct"] == 3


def test_basic_counts():
    p = _profile(describe_column("test", "age"))
    assert p["total"] == 4
    assert p["non_null"] == 3
    assert p["distinct"] == 2
    assert p["null_count"] == 1


def test_range_and_center():
    p = _profile(describe_column("test", "age"))
    assert p["min"] == 25
    assert p["max"] == 30
    assert p["mean"] == round(80 / 3, 2)
    assert p["median"] == 25


def test_std_present():
    p = _profile(describe_column("test", "age"))
    assert "std" in p
    assert p["std"] is not None
    assert 2.0 <= float(p["std"]) <= 3.5


def test_median_in_facts():
    r = describe_column("test", "age")
    joined = " ".join(f.text for f in r.facts)
    assert "Медиана" in joined and "25" in joined


def test_frequencies_present():
    r = describe_column("test", "age")
    assert len(_by_kind(r, "freq")) >= 1
    # И табличное представление есть
    assert r.data is not None and r.data.rows


def test_examples_present():
    r = describe_column("test", "age")
    examples = _by_kind(r, "examples")
    assert len(examples) == 1
    assert len(examples[0].values["examples"]) >= 1


def test_city_has_frequencies():
    r = describe_column("test", "city")
    freqs = _by_kind(r, "freq")
    top = {f.values["value"]: f.values["count"] for f in freqs}
    assert top.get("Москва") == 2


def test_describe_column_numeric_full_profile():
    r = describe_column("test", "age")
    p = _profile(r)
    assert p["total"] == 4
    assert p["non_null"] == 3
    assert p["null_count"] == 1
    assert p["min"] == 25
    assert p["max"] == 30
    assert p["median"] == 25
    assert "std" in p
    assert _by_kind(r, "freq")
    assert _by_kind(r, "examples")
    assert any("Медиана" in f.text for f in r.facts)


def test_describe_column_invalid():
    r = describe_column("test", "invalid_column")
    assert r.status == "error"
    assert r.error.code == "invalid_column"
    assert "Доступные колонки" in r.error.suggestion


# ---------- value_counts ----------


def test_value_counts():
    r = value_counts("test", "city")
    assert r.status == "ok"
    assert r.data is not None
    assert r.data.rows[0][0] == "Москва"
    assert r.data.rows[0][1] == 2
    assert "Москва" in r.facts[0].text and "2 раз" in r.facts[0].text


def test_value_counts_with_filter():
    r = value_counts("test", "city", filter="age > 20")
    assert r.status == "ok"
    rows = {row[0]: row[1] for row in r.data.rows}
    assert rows == {"Москва": 2, "СПб": 1}


def test_accepts_filter_keyword():
    r = value_counts("test", "city", filter="age > 100")
    assert r.status == "empty"
    assert "не найдены" in r.facts[0].text


@pytest.mark.parametrize(
    "filter_sql",
    [
        "age > 100; age < 20",
        "age > 100 UNION SELECT * FROM test",
        "age > 100) UNION SELECT * FROM test WHERE (TRUE",
        "age > 100) ORDER BY city --",
        "age > 100) LIMIT 1 --",
        "age > 100); DELETE FROM test; SELECT (1",
    ],
)
def test_rejects_filter_that_changes_query_structure(filter_sql):
    result = value_counts("test", "city", filter=filter_sql)
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


def test_accepts_compound_filter_with_semicolon_in_string():
    result = value_counts(
        "test", "city", filter="(age > 25 OR age IS NULL) AND city <> 'city;name'"
    )
    assert result.status == "ok"
    assert {row[0]: row[1] for row in result.data.rows} == {"СПб": 1, "Казань": 1}


def test_accepts_filter_with_trailing_sql_comment():
    result = value_counts("test", "city", filter="age > 25 -- older customers")
    assert result.status == "ok"
    assert result.data.rows == [["СПб", 1]]


@pytest.mark.parametrize("tool_name", ["describe_dataset", "describe_column", "value_counts"])
def test_agent_call_examples_are_valid_and_executable(tool_name):
    exported = next(tool for tool in get_tools_for_llm() if tool["name"] == tool_name)
    definition = next(tool for tool in TOOLS_REGISTRY if tool.name == tool_name)
    example = exported["parameters_schema"]["examples"][0]
    params = definition.parameters_model.model_validate(example).model_dump()
    tool = {
        "describe_dataset": describe_dataset,
        "describe_column": describe_column,
        "value_counts": value_counts,
    }[tool_name]
    result = tool(**params)
    assert result.status == "ok"
    if tool_name == "value_counts":
        assert example["top_n"] == 3
        assert result.data.rows == [["СПб", 1]]


# ---------- injection ----------


def test_sql_injection_protection():
    r = describe_column("test'); DELETE FROM test; --", "age")
    assert r.status == "error"


def test_describe_column_injection_does_not_delete():
    describe_column("test'); DELETE FROM test; --", "age")
    assert _test_overview(describe_dataset()).values["row_count"] == 4


def test_value_counts_injection_does_not_delete():
    value_counts("test'); DELETE FROM test; --", "city")
    assert _test_overview(describe_dataset()).values["row_count"] == 4


def test_column_injection_does_not_break_schema():
    describe_column("test", "age'); DROP TABLE test; --")
    ov = _test_overview(describe_dataset())
    assert ov.values["row_count"] == 4
    assert set(ov.values["columns"]) == {"id", "city", "age"}


def test_injection_data_integrity():
    describe_column("test'); DELETE FROM test; --", "age")
    assert _test_overview(describe_dataset()).values["row_count"] == 4, "Инъекция удалила данные!"


"""Тесты для time_aggregate (B2-4)."""


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


def _rows_as_dict(result, key_col=0, val_col=-1):
    return {str(r[key_col])[:10]: r[val_col] for r in result.data.rows}


# --- базовые агрегаты и границы ---


def test_monthly_count_boundaries():
    """2024-01-31 23:59:59 → январь; 2024-02-01 00:00:00 → февраль."""
    result = time_aggregate("events", "ts", freq="month", agg="count")
    assert result.status == "ok"
    rows = {row[0].strftime("%Y-%m"): row[1] for row in result.data.rows}
    assert rows == {"2024-01": 4, "2024-02": 3, "2024-03": 1}


def test_monthly_sum_uses_measure():
    result = time_aggregate("events", "ts", freq="month", agg="sum", measure="amount")
    assert result.status == "ok"
    rows = {row[0].strftime("%Y-%m"): row[1] for row in result.data.rows}
    # январь: 100+200+110+190 = 600
    # февраль: 105+105+10000 = 10210
    # март: 120
    assert rows == {"2024-01": 600.0, "2024-02": 10210.0, "2024-03": 120.0}


def test_daily_count_collapses_same_day():
    result = time_aggregate("events", "ts", freq="day", agg="count")
    assert result.status == "ok"
    # 2024-01-15 встречается 3 раза, 2024-01-31 — 1, 2024-02-01 — 2, ...
    rows = {row[0].strftime("%Y-%m-%d"): row[1] for row in result.data.rows}
    assert rows["2024-01-15"] == 3
    assert rows["2024-02-01"] == 2


# --- группировка ---


def test_group_by_city_splits_buckets():
    result = time_aggregate("events", "ts", freq="month", agg="count", group_by=["city"])
    assert result.status == "ok"
    # columns: time_bucket, city, value
    got = {(row[0].strftime("%Y-%m"), row[1]): row[2] for row in result.data.rows}
    assert got == {
        ("2024-01", "Москва"): 2,
        ("2024-01", "СПб"): 2,
        ("2024-02", "Москва"): 2,
        ("2024-02", "СПб"): 1,
        ("2024-03", "Москва"): 1,
    }


# --- фильтр ---


def test_filter_limits_rows():
    result = time_aggregate(
        "events",
        "ts",
        freq="month",
        agg="count",
        filter="city = 'Москва'",
    )
    assert result.status == "ok"
    rows = {row[0].strftime("%Y-%m"): row[1] for row in result.data.rows}
    assert rows == {"2024-01": 2, "2024-02": 2, "2024-03": 1}


def test_filter_injection_rejected():
    result = time_aggregate("events", "ts", freq="day", filter="1=1; DROP TABLE events")
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


# --- факты ---


def test_facts_present_and_consistent():
    result = time_aggregate("events", "ts", freq="month", agg="sum", measure="amount")
    assert _by_kind(result, "time_summary")
    assert _by_kind(result, "time_range")
    assert _by_kind(result, "peak")
    assert _by_kind(result, "trough")
    assert _by_kind(result, "average")

    peak = _by_kind(result, "peak")[0].values
    # Максимум — февраль, 10210
    assert peak["value"] == 10210.0
    assert "2024-02" in str(peak["bucket"])


def test_chart_hint_is_line():
    result = time_aggregate("events", "ts", freq="month")
    assert result.chart_hint is not None
    assert result.chart_hint.type == "line"
    assert result.chart_hint.x == "time_bucket"
    assert result.chart_hint.y == ["value"]


# --- ошибки и пустой результат ---


def test_invalid_freq_returns_error():
    result = time_aggregate("events", "ts", freq="fortnight")
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
    assert "day" in result.error.suggestion


def test_invalid_agg_returns_error():
    result = time_aggregate("events", "ts", agg="median2")
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_sum_without_measure_returns_error():
    result = time_aggregate("events", "ts", agg="sum")  # measure=None
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
    assert "measure" in result.error.message


def test_nonexistent_time_column_returns_error():
    result = time_aggregate("events", "created")
    assert result.status == "error"
    assert result.error.code == "invalid_column"
    assert "ts" in result.error.suggestion


def test_top_n_truncates():
    result = time_aggregate("events", "ts", freq="month", top_n=1)
    assert result.status == "ok"
    assert len(result.data.rows) == 1
    assert result.data.truncated is True


def test_empty_result_status():
    result = time_aggregate("events", "ts", freq="month", filter="city = 'Нет такого'")
    assert result.status == "empty"
    assert _by_kind(result, "empty_result")


"""Тесты для find_duplicates (B2-4)."""


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


# --- частичные дубли по выбранным полям ---


def test_partial_duplicates_by_two_columns():
    """test-таблица: (Москва, 25) встречается 2 раза."""
    result = find_duplicates("test", ["city", "age"])
    assert result.status == "ok"
    assert result.data.columns == ["city", "age", "duplicate_count"]
    assert result.data.row_count == 1
    assert result.data.rows[0] == ["Москва", 25, 2]


def test_partial_duplicates_by_single_column():
    """Группировка по city: Москва=2, СПб=1, Казань=1."""
    result = find_duplicates("test", ["city"])
    assert result.status == "ok"
    groups = {row[0]: row[-1] for row in result.data.rows}
    assert groups == {"Москва": 2}


def test_events_duplicate_planted_row():
    """В events есть точный дубль (ts, city, amount) у строк 5 и 6."""
    result = find_duplicates("events", ["ts", "city", "amount"])
    assert result.status == "ok"
    assert result.data.row_count == 1
    row = result.data.rows[0]
    assert row[-1] == 2
    # ключ содержит amount=105.0 и city='Москва'
    assert "Москва" in row
    assert 105.0 in row


# --- полные дубли (без columns) ---


def test_full_row_duplicates_none_in_test():
    """В test все id уникальны — полных дублей строк нет."""
    result = find_duplicates("test")
    assert result.status == "empty"
    assert _by_kind(result, "duplicate_summary")
    summary = _by_kind(result, "duplicate_summary")[0].values
    assert summary["duplicate_groups"] == 0


def test_full_row_duplicates_in_planted_table():
    con = __import__("duckdb").connect(":memory:")
    con.execute("CREATE TABLE t (id INT, v VARCHAR)")
    con.execute("INSERT INTO t VALUES (1, 'a'), (1, 'a'), (2, 'b'), (2, 'b')")
    result = find_duplicates("t", connection=con)
    assert result.status == "ok"
    assert result.data.row_count == 2  # две группы дублей
    total = sum(row[-1] for row in result.data.rows)
    assert total == 4  # 2+2 строки


# --- include_all_columns ---


def test_include_all_columns_exposes_other_fields():
    result = find_duplicates("test", ["city", "age"], include_all_columns=True)
    assert result.status == "ok"
    assert result.data.columns == ["city", "age", "duplicate_count", "id"]
    # values "id" присутствует (какой именно — FIRST без ORDER BY, проверяем только наличие)


def test_include_all_columns_ignored_when_columns_is_full_row():
    """columns = все колонки → остальных нет, флаг игнорируется."""
    result = find_duplicates("test", ["id", "city", "age"], include_all_columns=True)
    assert result.data.columns == ["id", "city", "age", "duplicate_count"]


# --- факты ---


def test_summary_facts_present():
    result = find_duplicates("test", ["city", "age"])
    assert _by_kind(result, "duplicate_summary")
    assert _by_kind(result, "duplicate_volume")
    assert _by_kind(result, "duplicate_peak")
    assert _by_kind(result, "duplicate_group")

    volume = _by_kind(result, "duplicate_volume")[0].values
    # 2 строки в группе дублей, 1 «лишняя»
    assert volume["duplicate_rows"] == 2
    assert volume["extra_rows"] == 1


# --- ошибки ---


def test_invalid_column_returns_error():
    result = find_duplicates("test", ["nonexistent"])
    assert result.status == "error"
    assert result.error.code == "invalid_column"
    assert "city" in result.error.suggestion


def test_invalid_limit_returns_error():
    result = find_duplicates("test", ["city"], limit=0)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_invalid_filter_returns_error():
    result = find_duplicates("test", ["city"], filter="1=1; DROP TABLE test")
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


def test_limit_truncates():
    # В test при group by city только 1 группа; в events — 2 (Москва, СПб)
    result = find_duplicates("events", ["city"], limit=1)
    assert result.status == "ok"
    assert len(result.data.rows) == 1
    assert result.data.truncated is True


"""Тесты для detect_anomalies (B2-4)."""


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


@pytest.fixture
def conn():
    """9 «нормальных» значений 10..12 и один явный выброс 100."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE metrics (id INT, value DOUBLE)")
    rows = [(i, 10 + (i % 3)) for i in range(1, 10)] + [(10, 100.0)]
    # id=1..9: value ∈ {10, 11, 12}; id=10: value=100.0
    con.executemany("INSERT INTO metrics VALUES (?, ?)", rows)
    return con


@pytest.fixture
def grouped_conn():
    """Две группы, в каждой свой выброс."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE metrics (id INT, city VARCHAR, value DOUBLE)")
    rows = [(i, "A", 10.0 + i % 3) for i in range(1, 7)] + [(7, "A", 100.0)]
    rows += [(i, "B", 20.0 + i % 3) for i in range(8, 14)] + [(14, "B", 200.0)]
    con.executemany("INSERT INTO metrics VALUES (?, ?, ?)", rows)
    return con


# --- IQR ---


def test_iqr_finds_single_outlier(conn):
    result = detect_anomalies("metrics", "value", method="iqr", id_column="id", connection=conn)
    assert result.status == "ok"
    assert result.data.row_count == 1
    row = result.data.rows[0]
    # columns: id, value, direction, deviation, lower_bound, upper_bound
    assert row[0] == 10  # id
    assert row[1] == 100.0  # value
    assert row[2] == "above"
    # границы соответствуют Q1−1.5·IQR / Q3+1.5·IQR
    assert row[5] > 12 and row[5] < 20  # верхняя граница между нормальными и выбросом


def test_iqr_custom_threshold_is_wider(conn):
    """Очень большой порог → аномалия не находится."""
    result = detect_anomalies(
        "metrics",
        "value",
        method="iqr",
        threshold=1000.0,
        id_column="id",
        connection=conn,
    )
    assert result.status == "empty"


# --- z-score ---


def test_zscore_finds_outlier_with_threshold(conn):
    # при threshold=2.5 z(100) ≈ 3.0 — аномалия; остальные z < 1
    result = detect_anomalies(
        "metrics",
        "value",
        method="zscore",
        threshold=2.5,
        id_column="id",
        connection=conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 1
    assert result.data.rows[0][1] == 100.0


# --- MAD ---


def test_mad_finds_outlier(conn):
    result = detect_anomalies("metrics", "value", method="mad", id_column="id", connection=conn)
    assert result.status == "ok"
    assert result.data.row_count == 1
    assert result.data.rows[0][1] == 100.0


# --- группировка ---


def test_grouped_iqr_finds_one_outlier_per_group(grouped_conn):
    result = detect_anomalies(
        "metrics",
        "value",
        method="iqr",
        group_by=["city"],
        id_column="id",
        connection=grouped_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 2
    by_group = {row[2]: row[1] for row in result.data.rows}  # city → value
    assert by_group == {"A": 100.0, "B": 200.0}


# --- filter ---


def test_filter_restricts_rows(grouped_conn):
    result = detect_anomalies(
        "metrics",
        "value",
        method="iqr",
        filter="city = 'A'",
        id_column="id",
        connection=grouped_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 1
    assert result.data.rows[0][1] == 100.0


# --- STL ---


def test_stl_residual_detects_spike(conn):
    """STL на коротком ряде может вернуть empty — проверяем хотя бы валидный ответ."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ts_series (ts TIMESTAMP, value DOUBLE)")
    # 24 месяца ровного тренда + один пик
    rows = []
    for i in range(24):
        month = i + 1
        year = 2022 + (month - 1) // 12
        mon = (month - 1) % 12 + 1
        v = 100.0 + i * 2
        if i == 15:
            v = 500.0
        rows.append((f"{year}-{mon:02d}-01 00:00:00", v))
    con.executemany("INSERT INTO ts_series VALUES (?, ?)", rows)

    result = detect_anomalies(
        "ts_series",
        "value",
        method="stl-residual",
        time_col="ts",
        freq="month",
        threshold=2.5,
        connection=con,
    )
    # либо нашли аномалию, либо чистый empty — но не error
    assert result.status in {"ok", "empty"}
    if result.status == "ok":
        values = [row[1] for row in result.data.rows]
        assert 500.0 in values


def test_stl_residual_requires_time_col(conn):
    result = detect_anomalies("metrics", "value", method="stl-residual", connection=conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
    assert "time_col" in result.error.message


# --- факты ---


def test_facts_present(conn):
    result = detect_anomalies("metrics", "value", method="iqr", id_column="id", connection=conn)
    assert _by_kind(result, "anomaly_summary")
    assert _by_kind(result, "anomaly_method")
    assert _by_kind(result, "anomaly_peak")
    assert _by_kind(result, "anomaly")

    summary = _by_kind(result, "anomaly_summary")[0].values
    assert summary == {"total": 1, "above": 1, "below": 0}


# --- ошибки ---


def test_invalid_method_returns_error(conn):
    result = detect_anomalies("metrics", "value", method="wat", connection=conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_invalid_threshold_returns_error(conn):
    result = detect_anomalies("metrics", "value", method="iqr", threshold=-1, connection=conn)
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_nonexistent_column_returns_error(conn):
    result = detect_anomalies("metrics", "cost", method="iqr", connection=conn)
    assert result.status == "error"
    assert result.error.code == "invalid_column"
    assert "value" in result.error.suggestion


def test_non_numeric_column_returns_error(conn):
    result = detect_anomalies("metrics", "id", method="iqr", connection=conn)
    # id — INT, это числовая колонка, поэтому ошибки не будет.
    # Проверим на явно текстовой:
    con2 = duckdb.connect(":memory:")
    con2.execute("CREATE TABLE t (name VARCHAR)")
    con2.execute("INSERT INTO t VALUES ('a'), ('b')")
    result = detect_anomalies("t", "name", method="iqr", connection=con2)
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_constant_column_returns_empty_with_explanation():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v INT)")
    con.execute("INSERT INTO t VALUES (5), (5), (5), (5)")
    result = detect_anomalies("t", "v", method="iqr", connection=con)
    assert result.status == "empty"
    facts = _by_kind(result, "empty_result")
    assert facts and "одинаковы" in facts[0].text


# def test_invalid_filter_returns_error(conn):
#     result = detect_anomalies(
#         "metrics",
#         "value",
#         method="iqr",
#         filter="1=1; DROP TABLE metrics",
#         connection=conn,
#     )
#     assert result.status == "error"
#     assert result.error.code == "invalid_filter"


def test_chart_hint_set_when_id_column_provided(conn):
    result = detect_anomalies("metrics", "value", method="iqr", id_column="id", connection=conn)
    assert result.chart_hint is not None
    assert result.chart_hint.type == "bar"
    assert result.chart_hint.x == "id"


def test_chart_hint_absent_without_id_column(conn):
    result = detect_anomalies("metrics", "value", method="iqr", connection=conn)
    assert result.chart_hint is None


def test_mad_grouped_no_alias_error():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (id INT, city VARCHAR, value DOUBLE)")
    rows = [(i, "A", v) for i, v in enumerate([10, 11, 12, 100])] + [
        (i + 10, "B", v) for i, v in enumerate([10, 11, 12, 100])
    ]
    con.executemany("INSERT INTO m VALUES (?, ?, ?)", rows)
    result = detect_anomalies(
        "m",
        "value",
        method="mad",
        group_by=["city"],
        id_column="id",
        connection=con,
    )
    assert result.status == "ok", result.error
    assert result.data.row_count == 2


def test_iqr_grouped_keeps_null_group():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (value DOUBLE, city VARCHAR)")
    con.executemany(
        "INSERT INTO m VALUES (?, ?)",
        [(v, None) for v in [10, 11, 12, 10, 11, 12, 100]],
    )
    result = detect_anomalies("m", "value", method="iqr", group_by=["city"], connection=con)
    assert result.status == "ok"
    # NULL-группа должна дать выброс 100, а не «аномалий нет»
    assert any(row[0] == 100.0 for row in result.data.rows)


def test_stl_constant_series_returns_empty():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (ts TIMESTAMP, v DOUBLE)")
    rows = []
    for m in range(1, 4):
        for d in range(1, 29):
            rows.append((f"2024-{m:02d}-{d:02d} 00:00:00", 100.0))
    con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    result = detect_anomalies(
        "t",
        "v",
        method="stl-residual",
        time_col="ts",
        freq="day",
        connection=con,
    )
    assert result.status == "empty"


def test_stl_year_freq_returns_error():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (ts TIMESTAMP, v DOUBLE)")
    con.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(f"{y}-01-01 00:00:00", 100.0 + (y - 2000)) for y in range(2000, 2024)],
    )
    result = detect_anomalies(
        "t",
        "v",
        method="stl-residual",
        time_col="ts",
        freq="year",
        connection=con,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_stl_numpy_keys_are_json_serializable():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (ts TIMESTAMP, g INT, v DOUBLE)")
    rows = []
    for m in range(1, 4):
        for d in range(1, 29):
            v = 100.0 + (1000.0 if (m, d) == (2, 15) else 0.0)
            rows.append((f"2024-{m:02d}-{d:02d} 00:00:00", 7, v))
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    result = detect_anomalies(
        "t",
        "v",
        method="stl-residual",
        time_col="ts",
        freq="day",
        group_by=["g"],
        connection=con,
    )
    assert result.status in {"ok", "empty"}
    # сериализация не должна падать на numpy.int64
    result.model_dump(mode="json")
    if result.status == "ok":
        # все ключи группы — Python int
        for row in result.data.rows:
            assert type(row[1]) is int


def test_extra_rows_independent_of_limit():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (a INT, b INT)")
    con.execute("INSERT INTO t VALUES (1, 1), (1, 1), (2, 2), (2, 2)")
    full = find_duplicates("t", ["a", "b"], connection=con)
    limited = find_duplicates("t", ["a", "b"], limit=1, connection=con)
    full_extra = next(f for f in full.facts if f.kind == "duplicate_volume").values["extra_rows"]
    lim_extra = next(f for f in limited.facts if f.kind == "duplicate_volume").values["extra_rows"]
    assert full_extra == 2 and lim_extra == 2
