"""Потери типизации на популяции расчёта: SUM, фильтр, JOIN, CTE и порог (B2-10).

Эталоны считаются по строкам генератора ``quality_orders`` модулем Python, а не SQL.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from djedai.analytics.quality import assess
from djedai.analytics.tools import run_sql
from djedai.contracts.tool_result import ToolResult


@pytest.fixture
def query(quality_dataset):
    connection, catalog = quality_dataset
    allowed = {t.table_id for t in catalog.tables}

    def run(sql: str):
        result = run_sql(sql, connection=connection, allowed_tables=allowed)
        assert result.status == "ok", result.error
        return assess(result, connection, catalog)

    return run


def amount(row: dict[str, str]) -> Decimal | None:
    try:
        return Decimal(row["amount"].replace(",", "."))
    except ArithmeticError:
        return None


def column(result, name: str):
    found = [c for c in result.quality.columns if c.column == name]
    assert len(found) == 1, result.quality
    return found[0]


def test_typed_sum_skips_unparsed_values_and_says_so(query, quality_spec):
    spec = quality_spec
    rows = spec.orders
    expected = sum(v for v in map(amount, rows) if v is not None)
    result = query("SELECT SUM(amount) FROM orders")
    assert Decimal(str(result.data.rows[0][0])) == expected

    quality = column(result, "amount")
    assert (quality.scope, quality.rows, quality.source_empty) == ("population", 103, 3)
    assert (quality.non_empty, quality.parse_failed, quality.failed_ratio) == (100, 4, 0.04)
    assert quality.usage == ["measure"]
    assert quality.raw_table == "raw_orders"
    assert quality.transformation == "parse_number"
    assert sorted(quality.examples) == sorted(spec.failed.values())
    (issue,) = quality.issues
    assert (issue.code, issue.severity) == ("parse_losses", "warning")
    assert "4 из 100" in issue.message and "4%" in issue.message
    facts = [f for f in result.facts if f.kind == "data_quality"]
    assert facts and facts[0].values["parse_failed"] == 4


def test_filter_population_is_counted_not_taken_from_profile(query, quality_spec):
    spec = quality_spec
    # в north (нечётные) из нераспознанных — 5 и 7, из пустых — 1 и 3
    result = query("SELECT SUM(amount) FROM orders WHERE region = 'north'")
    quality = column(result, "amount")
    north = [r for r in spec.orders if r["region"] == "north"]
    assert quality.scope == "population"
    assert quality.rows == len(north)
    assert quality.source_empty == sum(1 for i in spec.empty if i % 2)
    assert quality.parse_failed == sum(1 for i in spec.failed if i % 2) == 2


def test_population_without_losses_has_no_warning(query):
    result = query("SELECT SUM(amount) FROM orders WHERE order_id >= 'o010'")
    assert result.quality.columns == []
    assert "orders.amount" in result.quality.checked
    assert not [f for f in result.facts if f.kind == "data_quality"]


def test_inner_join_counts_joined_rows(query, quality_spec):
    spec = quality_spec
    result = query(
        "SELECT SUM(o.amount * i.qty) FROM orders o JOIN items i ON o.order_id = i.order_id"
    )
    quality = column(result, "amount")
    # у заказов 1–60 по две позиции: каждая строка заказа повторена дважды
    assert quality.rows == 2 * spec.items_until
    assert quality.source_empty == 2 * len(spec.empty)
    assert quality.parse_failed == 2 * len(spec.failed)


def test_left_join_unmatched_rows_are_not_losses(query, quality_spec):
    spec = quality_spec
    result = query(
        "SELECT SUM(o.amount) FROM items i LEFT JOIN orders o "
        "ON o.order_id = i.order_id AND o.region = 'north'"
    )
    quality = column(result, "amount")
    matched = 2 * sum(1 for i in range(1, spec.items_until + 1) if i % 2)
    assert quality.rows == matched
    assert quality.parse_failed == 2 * 2  # заказы 5 и 7


def test_cte_passthrough_uses_outer_filter(query, quality_spec):
    spec = quality_spec
    result = query(
        "WITH o AS (SELECT order_id, region, amount AS a FROM orders) "
        "SELECT SUM(a) FROM o WHERE region = 'south'"
    )
    quality = column(result, "amount")
    assert quality.scope == "population"
    assert quality.parse_failed == 2  # заказы 4 и 6
    assert quality.rows == sum(1 for r in spec.orders if r["region"] == "south")


def test_aggregate_in_cte_counts_cte_rows(query):
    result = query(
        "WITH t AS (SELECT region, SUM(amount) AS total FROM orders GROUP BY region) "
        "SELECT MAX(total) FROM t"
    )
    quality = column(result, "amount")
    assert (quality.rows, quality.parse_failed) == (103, 4)


def test_derived_expression_counts_its_source(query):
    result = query("SELECT region, SUM(amount * 2) AS doubled FROM orders GROUP BY region")
    assert column(result, "amount").parse_failed == 4


def test_filter_on_unparsed_dates_reports_dropped_rows(query, quality_spec):
    spec = quality_spec
    result = query("SELECT COUNT(*) FROM orders WHERE order_date >= DATE '2024-01-01'")
    quality = column(result, "order_date")
    assert quality.usage == ["filter"]
    assert (quality.scope, quality.rows, quality.non_empty) == ("population", 103, 103)
    (issue,) = quality.issues
    assert issue.code == "parse_losses"
    assert f"{len(spec.bad_dates)} из 103 непустых" in issue.message


def test_filter_that_keeps_unparsed_rows_reports_nothing(query):
    result = query("SELECT COUNT(*) FROM orders WHERE order_date IS NULL OR region = 'south'")
    # у заказов 10 и 11 дата не распознана: IS NULL их оставляет
    assert result.quality.columns == []


def test_correlated_subquery_falls_back_to_profile_with_boundary(query):
    result = query(
        "SELECT order_id FROM orders o WHERE o.amount > "
        "(SELECT AVG(o2.amount) FROM orders o2 WHERE o2.region = o.region)"
    )
    profile = [c for c in result.quality.columns if c.scope == "table"]
    assert profile and all(c.boundary for c in profile)
    assert "по всей таблице" in profile[0].issues[0].message


def test_exactly_threshold_column_is_typed_and_warns(query):
    result = query("SELECT SUM(v95) FROM thresholds")
    quality = column(result, "v95")
    assert (quality.non_empty, quality.parse_failed) == (100, 5)
    assert quality.issues[0].code == "parse_losses"


def test_column_below_threshold_is_text_and_warns_when_cast(query):
    result = query("SELECT SUM(TRY_CAST(v94 AS INTEGER)) FROM thresholds")
    quality = column(result, "v94")
    (issue,) = quality.issues
    assert (issue.code, issue.severity) == ("kept_as_text", "warning")
    assert "94%" in issue.message and "95%" in issue.message


def test_source_nulls_alone_are_not_losses(query):
    result = query("SELECT SUM(amount) FROM orders WHERE order_id IN ('o001', 'o002', 'o020')")
    assert result.quality.columns == []


def test_unknown_column_gives_partial_lineage_and_table_profile(quality_dataset):
    connection, catalog = quality_dataset
    result = ToolResult(result_id="r", status="ok", sql="SELECT SUM(nope) FROM orders")
    assessed = assess(result, connection, catalog)
    assert assessed.quality.lineage == "partial"
    assert assessed.quality.unresolved
    assert {c.column for c in assessed.quality.columns} == {"amount", "order_date"}
    assert all(c.scope == "table" for c in assessed.quality.columns)
    assert any("неполными" in f.text for f in assessed.facts)


def test_join_key_losses_are_reported_with_table_boundary(query, quality_spec):
    result = query("SELECT COUNT(*) FROM orders o JOIN orders p ON o.order_date = p.order_date")
    quality = column(result, "order_date")
    assert quality.usage == ["join_key"]
    assert (quality.scope, quality.parse_failed) == ("table", len(quality_spec.bad_dates))
    assert "JOIN" in quality.boundary
    (issue,) = quality.issues
    assert (issue.code, issue.severity) == ("parse_losses", "warning")
    assert any(f.kind == "data_quality" for f in result.facts)


@pytest.mark.parametrize(
    ("condition", "rows", "failed"),
    [("order_id >= 'o010'", 94, 0), ("region = 'north'", 52, 2)],
)
def test_aggregate_filter_limits_the_measure_population(query, condition, rows, failed):
    filtered = query(f"SELECT SUM(amount) FILTER (WHERE {condition}) FROM orders")
    where = query(f"SELECT SUM(amount) FROM orders WHERE {condition}")
    assert filtered.data.rows == where.data.rows
    if failed:
        quality = column(filtered, "amount")
        assert (quality.scope, quality.rows, quality.parse_failed) == ("population", rows, failed)
        assert column(where, "amount").parse_failed == failed
    else:
        assert filtered.quality.columns == where.quality.columns == []


def test_aggregate_filter_on_unparsed_dates_reports_dropped_rows(query, quality_spec):
    result = query("SELECT COUNT(*) FILTER (WHERE order_date >= DATE '2024-01-01') FROM orders")
    quality = column(result, "order_date")
    assert quality.usage == ["filter"]
    assert f"{len(quality_spec.bad_dates)} из 103 непустых" in quality.issues[0].message
