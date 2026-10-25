"""Lineage SQL до колонок каталога: JOIN, CTE, подзапросы, UNION и неполная связь (B2-10)."""

from __future__ import annotations

import pytest

from djedai.analytics.lineage import trace
from djedai.analytics.scope import parse_read_query


@pytest.fixture
def lineage(quality_dataset):
    _, catalog = quality_dataset

    def run(sql: str):
        return trace(parse_read_query(sql), catalog)

    return run


def uses(result) -> set[tuple[str, str, str]]:
    return {(u.table, u.column, u.usage) for u in result.uses}


def test_measure_filter_and_group_in_one_select(lineage):
    result = lineage(
        "SELECT region, SUM(amount) FROM orders WHERE order_date >= '2024-02-01' GROUP BY 1"
    )
    assert uses(result) == {
        ("orders", "region", "output"),
        ("orders", "region", "group"),
        ("orders", "amount", "measure"),
        ("orders", "order_date", "filter"),
    }
    assert result.unresolved == []
    (amount,) = [u for u in result.uses if u.column == "amount"]
    assert amount.aggregate == "sum"
    assert amount.target == len(result.scopes) - 1  # корень


def test_join_marks_keys_and_resolves_unqualified_columns(lineage):
    result = lineage(
        "SELECT SUM(amount * qty) FROM orders o JOIN items i ON o.order_id = i.order_id"
    )
    assert uses(result) == {
        ("orders", "amount", "measure"),
        ("items", "qty", "measure"),
        ("orders", "order_id", "join_key"),
        ("items", "order_id", "join_key"),
    }
    measures = [u for u in result.uses if u.usage == "measure"]
    assert len({u.expression for u in measures}) == 1  # одно выражение — одна сумма


def test_cte_passthrough_keeps_outer_population(lineage):
    result = lineage(
        "WITH o AS (SELECT order_id, amount AS a FROM orders WHERE region = 'north') "
        "SELECT SUM(a) FROM o JOIN items USING (order_id)"
    )
    (amount,) = [u for u in result.uses if u.column == "amount"]
    assert amount.usage == "measure"
    root = len(result.scopes) - 1
    assert amount.target == root
    assert [hop.alias for hop in amount.path] == ["o", "orders"]
    assert ("orders", "region", "filter") in uses(result)


def test_aggregate_inside_cte_is_its_own_population(lineage):
    result = lineage(
        "WITH t AS (SELECT region, SUM(amount) AS total FROM orders GROUP BY region) "
        "SELECT MAX(total) FROM t"
    )
    (amount,) = [u for u in result.uses if u.column == "amount"]
    assert amount.usage == "measure"
    assert amount.aggregate == "sum"
    assert result.scopes[amount.target].expression.args.get("group") is not None
    assert amount.path[-1].alias == "orders"


def test_star_from_subquery_is_expanded(lineage):
    result = lineage("SELECT SUM(t.amount) FROM (SELECT * FROM orders) t")
    assert ("orders", "amount", "measure") in uses(result)
    assert ("orders", "region", "output") not in uses(result)  # не использована снаружи


def test_union_branches_are_traced(lineage):
    result = lineage("SELECT amount FROM orders UNION ALL SELECT qty FROM items")
    assert uses(result) == {("orders", "amount", "output"), ("items", "qty", "output")}


def test_union_inside_cte_has_boundary(lineage):
    result = lineage(
        "WITH u AS (SELECT amount AS v FROM orders UNION ALL SELECT qty FROM items) "
        "SELECT SUM(v) FROM u"
    )
    assert {(u.table, u.column) for u in result.uses} == {("orders", "amount"), ("items", "qty")}
    assert all(u.boundary for u in result.uses)


def test_correlated_subquery_has_boundary(lineage):
    result = lineage(
        "SELECT order_id FROM orders o WHERE amount > "
        "(SELECT AVG(o2.amount) FROM orders o2 WHERE o2.region = o.region)"
    )
    inner = [u for u in result.uses if u.aggregate == "avg"]
    assert inner and all(u.boundary for u in inner)


def test_unknown_column_makes_lineage_partial(lineage):
    result = lineage("SELECT SUM(nope) FROM orders")
    assert result.query is None
    assert result.unresolved and "nope" in result.unresolved[0]
    assert result.tables == {"orders"}
