import duckdb
import pytest

from djedai.analytics.tools import compare_segments


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


@pytest.fixture
def different_conn():
    """Группы A (mean≈10) и B (mean≈15); категориальная cat: A=x, B=y."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (seg VARCHAR, value DOUBLE, cat VARCHAR)")
    a_rows = [("A", 10.0 + i * 0.01, "x") for i in range(50)]
    b_rows = [("B", 15.0 + i * 0.01, "y") for i in range(50)]
    con.executemany("INSERT INTO m VALUES (?, ?, ?)", a_rows + b_rows)
    return con


@pytest.fixture
def same_conn():
    """Два идентичных распределения — значимого различия нет."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (seg VARCHAR, value DOUBLE)")
    a_rows = [("A", float(i)) for i in range(1, 51)]
    b_rows = [("B", float(i) + 0.1) for i in range(1, 51)]
    con.executemany("INSERT INTO m VALUES (?, ?)", a_rows + b_rows)
    return con


def test_significant_numeric_difference(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value"],
        connection=different_conn,
    )
    assert result.status == "ok"
    assert result.data.row_count == 1
    row = dict(zip(result.data.columns, result.data.rows[0], strict=False))
    assert row["kind"] == "numeric"
    assert row["n_a"] == 50 and row["n_b"] == 50
    assert row["significant"] is True
    assert row["p_value"] < 0.001
    assert abs(row["effect_size"]) > 0.8  # Cohen's d большой
    assert row["effect_size_name"] == "cohens_d"
    assert abs(row["stat_a"] - 10.245) < 0.1
    assert abs(row["stat_b"] - 15.245) < 0.1


def test_no_significant_difference(same_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value"],
        connection=same_conn,
    )
    assert result.status == "ok"
    row = dict(zip(result.data.columns, result.data.rows[0], strict=False))
    assert row["significant"] is False
    assert row["p_value"] > 0.05


def test_categorical_chi_square(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["cat"],
        connection=different_conn,
    )
    assert result.status == "ok"
    row = dict(zip(result.data.columns, result.data.rows[0], strict=False))
    assert row["kind"] == "categorical"
    assert row["significant"] is True
    assert row["effect_size_name"] == "cramers_v"
    assert row["stat_a"] == "x"
    assert row["stat_b"] == "y"


def test_facts_present_connection(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value", "cat"],
        connection=different_conn,
    )
    assert _by_kind(result, "compare_summary")
    assert _by_kind(result, "compare_numeric")
    assert _by_kind(result, "compare_categorical")


def test_missing_segment_returns_error(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "Z",
        measures=["value"],
        connection=different_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
    assert "Z" in result.error.message


def test_missing_column_returns_error(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["nonexistent"],
        connection=different_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_empty_measures_returns_error(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=[],
        connection=different_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_invalid_alpha_returns_error(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value"],
        alpha=1.5,
        connection=different_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"


def test_invalid_filter_returns_error_different_connection(different_conn):
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value"],
        filter="1=1; DROP TABLE m",
        connection=different_conn,
    )
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


def test_insufficient_data_reports_skip():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE m (seg VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO m VALUES ('A', 1.0), ('A', 2.0), ('B', 3.0)")
    result = compare_segments(
        "m",
        "seg",
        "A",
        "B",
        measures=["value"],
        connection=con,
    )
    assert result.status == "ok"
    row = dict(zip(result.data.columns, result.data.rows[0], strict=False))
    assert row["significant"] is False
    assert row["effect_size_name"] == "insufficient_data"
