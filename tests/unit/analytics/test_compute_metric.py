"""Расчёт подтверждённых метрик (B2-9) на orders/items/payments.

Эталоны считаются здесь же по CSV модулем ``csv``, без DuckDB и проверяемого кода.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from djedai.analytics.metrics import MetricContext, compute_metric
from djedai.analytics.registry import execute_tool
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.metrics import MetricDefinition
from djedai.data.service import CsvSource, ingest_csv_dataset

FIXTURES = Path(__file__).parents[2] / "fixtures" / "metrics"
CONFIRMED = {
    "source": "api",
    "by_user_id": "u_1",
    "at": "2026-10-25T09:00:00Z",
}


def rows(name: str) -> list[dict[str, str]]:
    with (FIXTURES / f"{name}.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


ORDERS = {o["zakaz_id"]: o for o in rows("orders")}
SEGMENTS = {c["klient_id"]: c["segment"] for c in rows("customers")}
PAYMENTS = rows("payments")


def in_2023(value: str) -> bool:
    return "2023-01-01" <= value < "2024-01-01"


def paid_orders(period: bool = False) -> list[dict[str, str]]:
    return [
        o
        for o in ORDERS.values()
        if o["status"] != "cancelled" and (not period or in_2023(o["data_zakaza"]))
    ]


def definition(metric_id: str, **fields) -> MetricDefinition:
    return MetricDefinition.model_validate(
        {
            "metric_id": metric_id,
            "title": metric_id,
            "status": "confirmed",
            "confirmation": CONFIRMED,
            **fields,
        }
    )


def money_sum(metric_id, table, key, measure, time, filters=(), **currency):
    return definition(
        metric_id,
        kind="sum",
        grain={"table": table, "key": key},
        measure={"table": table, "column": measure},
        additivity="additive",
        unit="money",
        time_column={"table": table, "column": time},
        filters=list(filters),
        **currency,
    )


def count(metric_id, table, key, time=None, filters=()):
    return definition(
        metric_id,
        kind="count",
        grain={"table": table, "key": key},
        additivity="additive",
        unit="count",
        time_column={"table": table, "column": time} if time else None,
        filters=list(filters),
    )


def ratio(metric_id, numerator, denominator, grain):
    return definition(
        metric_id,
        kind="ratio",
        grain=grain,
        numerator=numerator,
        denominator=denominator,
        additivity="recompute",
        unit="ratio",
    )


NOT_CANCELLED = {
    "column": {"table": "orders", "column": "status"},
    "op": "ne",
    "value": "cancelled",
}
PAYMENT_ONLY = {"column": {"table": "payments", "column": "tip"}, "op": "eq", "value": "payment"}
CURRENCY = {"table": "payments", "column": "valyuta"}
ORDER_GRAIN = {"table": "orders", "key": "zakaz_id"}
DEFINITIONS = [
    money_sum(
        "order_total", "orders", "zakaz_id", "summa_itogo", "data_zakaza", [NOT_CANCELLED],
        currency="RUB",
    ),
    money_sum(
        "gross_payments", "payments", "platezh_id", "summa", "data_platezha", [PAYMENT_ONLY],
        currency_column=CURRENCY,
    ),
    money_sum(
        "net_payments", "payments", "platezh_id", "summa", "data_platezha",
        currency_column=CURRENCY,
    ),
    count("orders_count", "orders", "zakaz_id", "data_zakaza", [NOT_CANCELLED]),
    count("customers_count", "customers", "klient_id"),
    count("visits_count", "visits", "vizit_id", "data_vizita"),
    ratio("average_check", "order_total", "orders_count", ORDER_GRAIN),
    ratio("orders_per_customer", "orders_count", "customers_count", ORDER_GRAIN),
    ratio("orders_per_visit", "orders_count", "visits_count", ORDER_GRAIN),
]  # fmt: skip


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    path = tmp_path_factory.mktemp("metrics") / "dataset.duckdb"
    catalog = ingest_csv_dataset(
        [CsvSource(p, p.name) for p in sorted(FIXTURES.glob("*.csv"))],
        path,
        dataset_id="ds_metrics",
        name="metrics",
        created_at=datetime.now(UTC),
    )
    with duckdb.connect(str(path), read_only=True) as connection:
        yield connection, catalog


@pytest.fixture
def semantics(dataset) -> MetricContext:
    _, catalog = dataset
    return MetricContext(catalog, {m.metric_id: m for m in DEFINITIONS})


@pytest.fixture
def compute(dataset, semantics):
    connection, catalog = dataset

    def run(metric_id: str, **params):
        return compute_metric(
            metric_id,
            connection=connection,
            allowed_tables={t.table_id for t in catalog.tables},
            semantics=semantics,
            **params,
        )

    return run


def value(result):
    assert result.status == "ok", result.error
    (row,) = result.data.rows
    return row[-1]


def table(result) -> dict:
    assert result.status == "ok", result.error
    return {tuple(row[:-1]) if len(row) > 2 else row[0]: row[-1] for row in result.data.rows}


def test_definitions_match_the_ingested_catalog(semantics):
    for metric in DEFINITIONS:
        metric.validate_against(semantics.catalog, semantics.metrics)


def test_undefined_metric_asks_for_definition(dataset, compute):
    result = compute("revenue")
    assert (result.status, result.error.code) == ("error", "metric_undefined")
    assert "order_total" in result.error.suggestion
    connection, _ = dataset
    without = compute_metric("order_total", connection=connection)
    assert without.error.code == "metric_undefined"


def test_order_total_respects_population_and_half_open_period(compute):
    assert value(compute("order_total")) == pytest.approx(
        sum(float(o["summa_itogo"]) for o in paid_orders())
    )
    result = compute("order_total", period_start="2023-01-01", period_end="2024-01-01")
    # 31.12.2023 23:59 входит, 01.01.2024 00:00 — уже следующий период
    expected = sum(float(o["summa_itogo"]) for o in paid_orders(period=True))
    assert value(result) == pytest.approx(expected) == pytest.approx(12000.0)
    provenance = result.metric
    assert (provenance.metric_id, provenance.version, provenance.currency) == (
        "order_total",
        1,
        "RUB",
    )
    assert (provenance.period_start, provenance.period_end) == ("2023-01-01", "2024-01-01")
    definition_fact = next(f for f in result.facts if f.kind == "metric_definition")
    assert "v1" in definition_fact.text and "[2023-01-01, 2024-01-01)" in definition_fact.text
    assert next(f for f in result.facts if f.kind == "metric").values["value"] == value(result)


def test_many_to_one_dimension_is_checked_and_kept(compute):
    result = compute(
        "order_total",
        group_by=["customers.segment"],
        period_start="2023-01-01",
        period_end="2024-01-01",
    )
    expected = defaultdict(float)
    for order in paid_orders(period=True):
        expected[SEGMENTS[order["klient_id"]]] += float(order["summa_itogo"])
    assert table(result) == pytest.approx(dict(expected))
    (check,) = result.metric.checks
    assert (check.max_fanout, check.null_key_rows, check.unmatched_rows) == (1, 0, 0)
    assert check.rows_before == check.rows_after == len(paid_orders(period=True))


def test_fanout_dimension_is_refused_without_distinct(compute):
    result = compute("order_total", group_by=["order_items.tovar"])
    assert result.error.code == "metric_grain_violation"
    assert "orders" in result.error.message


def test_null_keys_are_not_silently_dropped(compute):
    result = compute("gross_payments", currency="RUB", group_by=["orders.status"])
    assert result.error.code == "metric_rows_lost"
    assert "с пустым ключом 1" in result.error.message
    # Явно исключённые платежи без заказа — уже другая популяция, и она считается.
    explicit = compute(
        "gross_payments",
        currency="RUB",
        group_by=["orders.status"],
        filters=[{"column": "payments.zakaz_id", "op": "not_null"}],
    )
    expected = sum(
        float(p["summa"])
        for p in PAYMENTS
        if p["zakaz_id"] and p["tip"] == "payment" and p["valyuta"] == "RUB"
    )
    assert table(explicit) == {"paid": pytest.approx(expected)}
    assert len(explicit.metric.filters) == 2


def test_gross_and_net_payments_are_different_definitions(compute):
    gross = value(compute("gross_payments", currency="RUB"))
    net = value(compute("net_payments", currency="RUB"))
    rub = [p for p in PAYMENTS if p["valyuta"] == "RUB"]
    assert gross == pytest.approx(sum(float(p["summa"]) for p in rub if p["tip"] == "payment"))
    assert net == pytest.approx(sum(float(p["summa"]) for p in rub))
    assert gross - net == pytest.approx(1000.0)


def test_mixed_currencies_are_not_added(compute):
    result = compute("gross_payments")
    assert result.error.code == "metric_mixed_currency"
    assert "RUB, USD" in result.error.message
    usd = compute("gross_payments", currency="USD")
    assert (value(usd), usd.metric.currency) == (pytest.approx(4.0), "USD")
    split = compute("gross_payments", group_by=["payments.valyuta"])
    expected = defaultdict(float)
    for payment in PAYMENTS:
        if payment["tip"] == "payment":
            expected[payment["valyuta"]] += float(payment["summa"])
    assert table(split) == pytest.approx(dict(expected))
    assert split.metric.currency is None


def test_conversion_depends_on_its_denominator(compute):
    orders = len(paid_orders())
    per_customer = compute("orders_per_customer")
    per_visit = compute("orders_per_visit")
    assert value(per_customer) == pytest.approx(orders / len(SEGMENTS))
    assert value(per_visit) == pytest.approx(orders / len(rows("visits")))
    assert per_customer.data.columns == ["numerator", "denominator", "value"]
    assert [(c.metric_id, c.version) for c in per_customer.metric.components] == [
        ("orders_count", 1),
        ("customers_count", 1),
    ]
    # У знаменателя «клиенты» нет календаря: период к нему не применить без уточнения.
    period = {"period_start": "2023-01-01", "period_end": "2024-01-01"}
    assert compute("orders_per_customer", **period).error.code == "metric_period_unsupported"
    visits_2023 = [v for v in rows("visits") if in_2023(v["data_vizita"])]
    assert value(compute("orders_per_visit", **period)) == pytest.approx(
        len(paid_orders(period=True)) / len(visits_2023)
    )


def test_ratio_is_recomputed_per_group_not_averaged(compute):
    result = compute(
        "average_check",
        group_by=["customers.segment"],
        period_start="2023-01-01",
        period_end="2024-01-01",
    )
    totals, counts = defaultdict(float), defaultdict(int)
    for order in paid_orders(period=True):
        segment = SEGMENTS[order["klient_id"]]
        totals[segment] += float(order["summa_itogo"])
        counts[segment] += 1
    by_segment = {row[0]: row[-1] for row in result.data.rows}
    assert by_segment == pytest.approx({s: totals[s] / counts[s] for s in totals})
    assert result.metric.currency == "RUB"
    assert result.metric.additivity == "recompute"
    fact = next(f for f in result.facts if f.kind == "metric_definition")
    assert "не суммируются" in fact.text


def test_calendar_buckets_follow_the_metric_time_column(compute):
    result = compute(
        "order_total", period_start="2023-01-01", period_end="2024-01-01", time_grain="month"
    )
    expected = defaultdict(float)
    for order in paid_orders(period=True):
        expected[order["data_zakaza"][:7] + "-01"] += float(order["summa_itogo"])
    assert table(result) == pytest.approx(dict(expected))
    assert result.chart_hint.type == "line"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"period_start": "2024-01-01", "period_end": "2023-01-01"}, "invalid_parameter"),
        ({"period_start": "01.01.2023"}, "invalid_parameter"),
        ({"period_start": "2023-01-01T00:00:00+03:00"}, "invalid_parameter"),
        ({"time_grain": "decade"}, "invalid_parameter"),
        ({"group_by": ["segment"]}, "invalid_parameter"),
        ({"group_by": ["orders.amount"]}, "invalid_column"),
        (
            {"filters": [{"column": "orders.status", "op": "in", "value": "paid"}]},
            "invalid_parameter",
        ),
    ],
)
def test_invalid_requests_are_explained(compute, params, code):
    assert compute("order_total", **params).error.code == code


def test_stale_definition_is_not_used(dataset, semantics):
    connection, catalog = dataset
    data = catalog.model_dump(mode="json")
    orders = next(t for t in data["tables"] if t["table_id"] == "orders")
    orders["columns"] = [c for c in orders["columns"] if c["name"] != "summa_itogo"]
    data["transformations"] = [
        t for t in data["transformations"] if t.get("column") != "summa_itogo"
    ]
    changed = MetricContext(DatasetCatalog.model_validate(data), semantics.metrics)
    result = compute_metric("order_total", connection=connection, semantics=changed)
    assert result.error.code == "metric_definition_stale"


def test_registry_passes_semantics_to_compute_metric(dataset, semantics):
    connection, catalog = dataset
    result = execute_tool(
        "compute_metric",
        {"metric_id": "orders_count", "filters": [{"column": "orders.status", "op": "is_null"}]},
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
        semantics=semantics,
    )
    assert value(result) == 0
    assert result.params["filters"] == [{"column": "orders.status", "op": "is_null"}]
