"""Защита run_sql (B2-9): мера подтверждённой метрики не агрегируется после fanout/потерь."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from djedai.analytics.metrics import MetricContext
from djedai.analytics.registry import execute_tool
from djedai.analytics.tools import run_sql
from djedai.contracts.metrics import MetricDefinition

FIXTURES = Path(__file__).parents[2] / "fixtures" / "metrics"
CONFIRMED = {"source": "api", "by_user_id": "u_1", "at": "2026-10-25T09:00:00Z"}
METRICS = [
    MetricDefinition.model_validate(
        {
            "metric_id": "order_total",
            "title": "Сумма заказов",
            "status": "confirmed",
            "confirmation": CONFIRMED,
            "kind": "sum",
            "grain": {"table": "orders", "key": "zakaz_id"},
            "measure": {"table": "orders", "column": "summa_itogo"},
            "additivity": "additive",
            "unit": "money",
            "currency": "RUB",
        }
    ),
    MetricDefinition.model_validate(
        {
            "metric_id": "payments_total",
            "title": "Платежи",
            "status": "confirmed",
            "confirmation": CONFIRMED,
            "kind": "sum",
            "grain": {"table": "payments", "key": "platezh_id"},
            "measure": {"table": "payments", "column": "summa"},
            "additivity": "additive",
            "unit": "money",
            "currency_column": {"table": "payments", "column": "valyuta"},
        }
    ),
]


def rows(name: str) -> list[dict[str, str]]:
    with (FIXTURES / f"{name}.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


ORDERS = rows("orders")
ITEMS_PER_ORDER = Counter(item["zakaz_id"] for item in rows("order_items"))
SEGMENTS = {c["klient_id"]: c["segment"] for c in rows("customers")}


@pytest.fixture
def query(metrics_dataset):
    connection, catalog = metrics_dataset
    semantics = MetricContext(catalog, {m.metric_id: m for m in METRICS})
    tables = {t.table_id for t in catalog.tables}

    def run(sql: str, *, guarded: bool = True):
        return run_sql(
            sql,
            connection=connection,
            allowed_tables=tables,
            semantics=semantics if guarded else None,
        )

    return run


EXPANDED = (
    "WITH expanded AS (SELECT o.summa_itogo FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id) SELECT SUM(summa_itogo) FROM expanded"
)
NAIVE = (
    "SELECT SUM(o.summa_itogo) AS total FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id"
)


def test_naive_join_inflates_the_sum_and_is_refused(query):
    unguarded = query(NAIVE, guarded=False)
    inflated = sum(float(o["summa_itogo"]) * ITEMS_PER_ORDER[o["zakaz_id"]] for o in ORDERS)
    assert unguarded.data.rows == [[pytest.approx(inflated)]]
    assert inflated > sum(float(o["summa_itogo"]) for o in ORDERS)

    result = query(NAIVE)
    assert (result.status, result.error.code) == ("error", "metric_grain_violation")
    assert "order_total" in result.error.suggestion
    assert "order_items.zakaz_id не уникален" in result.error.message.replace("i.", "order_items.")


FANOUT = {
    "DISTINCT не спасает": "SELECT COUNT(DISTINCT o.summa_itogo) FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id",
    "SUM DISTINCT": "SELECT SUM(DISTINCT o.summa_itogo) FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id",
    "grain присоединён к позициям": "SELECT i.tovar, SUM(o.summa_itogo) FROM order_items i "
    "JOIN orders o ON o.zakaz_id = i.zakaz_id GROUP BY 1",
    "USING без алиасов": "SELECT SUM(summa_itogo) FROM orders JOIN order_items USING (zakaz_id)",
    "CROSS JOIN": "SELECT SUM(o.summa_itogo) FROM orders o, visits v",
    "условие не равенство": "SELECT SUM(o.summa_itogo) FROM orders o "
    "JOIN visits v ON v.data_vizita < o.data_zakaza",
    "CTE на grain и fanout": "WITH t AS (SELECT zakaz_id, SUM(summa_itogo) AS s FROM orders "
    "GROUP BY 1) SELECT SUM(t.s) FROM t JOIN order_items i ON i.zakaz_id = t.zakaz_id",
    "ветка UNION": "SELECT SUM(summa_itogo) FROM orders UNION ALL "
    "SELECT SUM(o.summa_itogo) FROM orders o JOIN visits v ON v.klient_id = o.klient_id",
    "окно": "SELECT SUM(o.summa_itogo) OVER () FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id",
    # JOIN уровнем ниже, SUM снаружи: размноженные строки приходят из CTE/подзапроса
    "JOIN в CTE": EXPANDED,
    "JOIN в подзапросе t.*": "SELECT SUM(x.summa_itogo) FROM (SELECT o.* FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id) x",
    "JOIN в CTE через *": "WITH e AS (SELECT * FROM orders o JOIN order_items i "
    "USING (zakaz_id)) SELECT SUM(summa_itogo) FROM e",
    "JOIN в CTE со списком колонок": "WITH e(x) AS (SELECT o.summa_itogo FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id) SELECT SUM(x) FROM e",
    "JOIN в подзапросе со списком колонок": "SELECT SUM(e.x) FROM (SELECT o.summa_itogo "
    "FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id) AS e(x)",
    "JOIN в CTE под алиасом": "WITH e AS (SELECT o.summa_itogo AS s FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id) SELECT SUM(s) FROM e",
    "JOIN двумя уровнями ниже": "WITH a AS (SELECT o.summa_itogo FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id), b AS (SELECT summa_itogo FROM a) "
    "SELECT SUM(summa_itogo) FROM b",
    "JOIN в ветке UNION": "WITH u AS (SELECT summa_itogo FROM orders UNION ALL "
    "SELECT o.summa_itogo FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id) "
    "SELECT SUM(summa_itogo) FROM u",
    "DISTINCT в CTE не исправляет fanout": "WITH e AS (SELECT DISTINCT o.zakaz_id, "
    "o.summa_itogo FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id) "
    "SELECT SUM(summa_itogo) FROM e",
}


@pytest.mark.parametrize("sql", FANOUT.values(), ids=FANOUT.keys())
def test_fanout_is_refused(query, sql):
    result = query(sql)
    assert result.status == "error", result.data
    assert result.error.code == "metric_grain_violation"


def test_inner_join_with_null_keys_is_refused(query):
    sql = (
        "SELECT o.status, SUM(p.summa) FROM payments p "
        "JOIN orders o ON o.zakaz_id = p.zakaz_id WHERE p.valyuta = 'RUB' GROUP BY 1"
    )
    result = query(sql)
    assert result.error.code == "metric_rows_lost"
    left = query(sql.replace("JOIN orders", "LEFT JOIN orders"))
    assert left.status == "ok", left.error


ALLOWED = {
    "N:1 без потерь": "SELECT c.segment, SUM(o.summa_itogo) FROM orders o "
    "JOIN customers c ON c.klient_id = o.klient_id GROUP BY 1",
    "соединение через WHERE": "SELECT SUM(o.summa_itogo) FROM orders o, customers c "
    "WHERE o.klient_id = c.klient_id",
    "агрегат до JOIN": "WITH t AS (SELECT zakaz_id, klient_id, SUM(summa_itogo) AS s "
    "FROM orders GROUP BY zakaz_id, klient_id) SELECT c.segment, SUM(t.s) FROM t "
    "JOIN customers c ON c.klient_id = t.klient_id GROUP BY 1",
    "позиции свёрнуты до заказа": "WITH i AS (SELECT zakaz_id, COUNT(*) AS n FROM order_items "
    "GROUP BY 1) SELECT SUM(o.summa_itogo) FROM orders o LEFT JOIN i ON i.zakaz_id = o.zakaz_id",
    "без JOIN": "SELECT SUM(summa_itogo) FROM orders",
    "мера не агрегируется": "SELECT o.zakaz_id, COUNT(*) FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id GROUP BY 1",
    "MAX не зависит от повторов": "SELECT MAX(o.summa_itogo) FROM orders o "
    "JOIN order_items i ON i.zakaz_id = o.zakaz_id",
    "колонка без метрики": "SELECT SUM(i.cena) FROM order_items i "
    "JOIN orders o ON o.zakaz_id = i.zakaz_id",
    "CTE без JOIN": "WITH e AS (SELECT * FROM orders) SELECT SUM(summa_itogo) FROM e",
    "CTE с измерением N:1": "WITH e AS (SELECT o.summa_itogo, c.segment FROM orders o "
    "JOIN customers c ON c.klient_id = o.klient_id) SELECT segment, SUM(summa_itogo) "
    "FROM e GROUP BY 1",
    "MAX на заказ до внешнего SUM": "WITH m AS (SELECT o.zakaz_id, MAX(o.summa_itogo) AS s "
    "FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id GROUP BY 1) "
    "SELECT SUM(s) FROM m",
}


@pytest.mark.parametrize("sql", ALLOWED.values(), ids=ALLOWED.keys())
def test_grain_preserving_queries_pass(query, sql):
    result = query(sql)
    assert result.status == "ok", result.error


def test_fanout_inside_cte_is_refused_not_inflated(query):
    total = sum(float(o["summa_itogo"]) for o in ORDERS)
    inflated = sum(float(o["summa_itogo"]) * ITEMS_PER_ORDER[o["zakaz_id"]] for o in ORDERS)
    assert query(EXPANDED, guarded=False).data.rows == [[pytest.approx(inflated)]]
    assert (total, inflated) == (pytest.approx(13200.0), pytest.approx(31900.0))
    result = query(EXPANDED)
    assert result.error.code == "metric_grain_violation"
    assert "order_items.zakaz_id не уникален" in result.error.message.replace("i.", "order_items.")
    per_order = query(ALLOWED["MAX на заказ до внешнего SUM"])
    assert per_order.data.rows == [[pytest.approx(total)]]


def test_allowed_dimension_query_matches_reference(query):
    result = query(ALLOWED["N:1 без потерь"])
    expected = defaultdict(float)
    for order in ORDERS:
        expected[SEGMENTS[order["klient_id"]]] += float(order["summa_itogo"])
    assert dict(map(tuple, result.data.rows)) == pytest.approx(dict(expected))


def test_without_confirmed_metrics_exploration_is_unchanged(query, metrics_dataset):
    connection, catalog = metrics_dataset
    empty = MetricContext(catalog, {})
    assert run_sql(NAIVE, connection=connection, semantics=empty).status == "ok"
    assert query(NAIVE, guarded=False).status == "ok"


def test_registry_guards_run_sql_with_dataset_semantics(metrics_dataset):
    connection, catalog = metrics_dataset
    semantics = MetricContext(catalog, {m.metric_id: m for m in METRICS})
    result = execute_tool(
        "run_sql",
        {"sql": NAIVE},
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
        semantics=semantics,
    )
    assert result.error.code == "metric_grain_violation"


def test_guard_failure_does_not_run_the_query(query, monkeypatch):
    def broken(*_args):
        raise AttributeError("unexpected SQL shape")

    monkeypatch.setattr("djedai.analytics.tools.metric_guard", broken)
    result = query(NAIVE)
    assert result.error.code == "metric_guard_failed"
    assert query(NAIVE, guarded=False).status == "ok"
