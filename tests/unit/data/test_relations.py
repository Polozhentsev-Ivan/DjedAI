"""Связи таблиц (B1-5) против эталонов, посчитанных независимо от DuckDB.

Таблицы собираются из Python-списков или берутся из синтетики PL-2; ожидаемые меры
пересечения, число строк и суммы после JOIN считаются по тем же данным через ``set``,
``Counter`` и ``Decimal``.
"""

from __future__ import annotations

import csv
import random
from collections import Counter
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from djedai.data.csv_ingest import load_raw_csv, load_raw_csvs
from djedai.data.profiler import profile_tables
from djedai.data.relations import RELATIONS_TABLE, find_relations, read_relations
from djedai.data.typed_views import create_typed_views

SYNTHETIC = Path(__file__).parents[2] / "fixtures" / "synthetic"


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def write_csv(path: Path, columns: dict[str, list[str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(columns)
        writer.writerows(zip(*columns.values(), strict=True))
    return path


def relations_of(con, tmp_path, tables: dict[str, dict[str, list[str]]]):
    for name, columns in tables.items():
        load_raw_csv(con, write_csv(tmp_path / f"{name}.csv", columns), header=True)
    create_typed_views(con)
    profile_tables(con)
    return find_relations(con)


def pairs(report) -> set[tuple[str, str, str, str]]:
    return {(r.from_table, r.from_column, r.to_table, r.to_column) for r in report.relations}


def ids(values) -> list[str]:
    return [str(v) for v in values]


def read_synthetic(name: str) -> list[dict[str, str]]:
    with (SYNTHETIC / f"{name}.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def rub(text: str) -> Decimal:  # "36 381,51 руб." → Decimal("36381.51")
    return Decimal(text.removesuffix(" руб.").replace(" ", "").replace(",", "."))


def test_synthetic_relations_are_found(con):
    load_raw_csvs(con, sorted(SYNTHETIC.glob("*.csv")))
    create_typed_views(con)
    profile_tables(con)
    report = find_relations(con)

    assert pairs(report) == {
        ("sales", "product_id", "products", "product_id"),
        ("sales", "store_id", "stores", "store_id"),
    }
    sales = read_synthetic("sales")
    total = sum(rub(row["total_amount"]) for row in sales)
    for relation in report.relations:
        target = {row[relation.to_column] for row in read_synthetic(relation.to_table)}
        source = {row[relation.from_column] for row in sales}
        assert relation.overlap == round(len(source & target) / len(source), 4)
        assert relation.jaccard == round(len(source & target) / len(source | target), 4)
        assert (relation.cardinality, relation.confidence) == ("N:1", "high")
        impact = relation.impact
        assert (impact.rows_before, impact.rows_after) == (len(sales), len(sales))
        assert (impact.unmatched_rows, impact.max_fanout) == (0, 1)
        amount = next(s for s in impact.sums if s.column == "total_amount")
        assert (Decimal(amount.before), Decimal(amount.after)) == (total, total)
    assert report.warnings == ()


def test_synthetic_sale_id_is_not_linked_to_product_id(con):
    """sale_id 1..4168 и product_id 1..50 — похожие ID, но не связь."""
    load_raw_csvs(con, sorted(SYNTHETIC.glob("*.csv")))
    create_typed_views(con)
    profile_tables(con)
    columns = {(r.from_column, r.to_column) for r in find_relations(con).relations}
    assert all("sale_id" not in pair and "date" not in pair for pair in columns)


def test_dense_id_ranges_are_not_a_relation(con, tmp_path):
    rng = random.Random(1)
    orders = range(1, 1001)
    buyers = [rng.randint(1, 100) for _ in orders]
    report = relations_of(
        con,
        tmp_path,
        {
            "customers": {"customer_id": ids(range(1, 101)), "name": [f"c{i}" for i in range(100)]},
            "orders": {"order_id": ids(orders), "customer_id": ids(buyers)},
        },
    )
    # order_id ⊇ customer_id (overlap 1.0), но Jaccard 0.1 и имена разные
    assert pairs(report) == {("orders", "customer_id", "customers", "customer_id")}
    relation = report.relations[0]
    assert relation.cardinality == "N:1"
    found = set(buyers) & set(range(1, 101))
    assert relation.overlap == round(len(found) / len(set(buyers)), 4)
    assert relation.jaccard == round(len(found) / len(set(buyers) | set(range(1, 101))), 4)


def test_ids_with_and_without_leading_zeros_do_not_match(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "stores": {"store_id": [f"{i:03d}" for i in range(1, 11)]},
            "sales": {"store_id": ids([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1, 2])},
        },
    )
    assert report.relations == ()


def test_one_to_one(con, tmp_path):
    users = list(range(1, 21))
    shuffled = users[:]
    random.Random(2).shuffle(shuffled)
    report = relations_of(
        con,
        tmp_path,
        {
            "users": {"user_id": ids(users), "login": [f"u{i}" for i in users]},
            "profiles": {"user_id": ids(shuffled), "age": ids(range(20, 40))},
        },
    )
    assert len(report.relations) == 1
    relation = report.relations[0]
    assert {relation.from_table, relation.to_table} == {"users", "profiles"}
    assert (relation.cardinality, relation.confidence) == ("1:1", "high")
    assert (relation.overlap, relation.jaccard) == (1.0, 1.0)
    assert relation.impact.rows_before == relation.impact.rows_after == 20


def test_many_to_many_join_multiplies_rows_and_sums(con, tmp_path):
    rng = random.Random(3)
    tagged = [rng.randint(1, 15) for _ in range(40)]
    priced = [rng.randint(1, 15) for _ in range(30)]
    tables = {
        "item_tags": {"item_id": ids(tagged), "qty": ids(rng.randint(1, 9) for _ in tagged)},
        "item_prices": {"item_id": ids(priced), "qty": ids(rng.randint(1, 9) for _ in priced)},
    }
    report = relations_of(con, tmp_path, tables)

    assert len(report.relations) == 1
    relation = report.relations[0]
    assert (relation.cardinality, relation.confidence) == ("N:M", "low")
    source, target = tables[relation.from_table], tables[relation.to_table]
    common = set(tagged) & set(priced)
    assert relation.overlap == round(len(common) / min(len(set(tagged)), len(set(priced))), 4)
    assert relation.jaccard == round(len(common) / len(set(tagged) | set(priced)), 4)

    per_key = Counter(target["item_id"])
    weights = [per_key.get(key, 1) for key in source["item_id"]]
    qty = [int(v) for v in source["qty"]]
    impact = relation.impact
    assert impact.rows_before == len(weights)
    assert impact.rows_after == sum(weights) > len(weights)
    assert impact.max_fanout == max(per_key[k] for k in source["item_id"] if k in per_key)
    assert impact.unmatched_rows == sum(k not in per_key for k in source["item_id"])
    (change,) = impact.sums
    assert (change.column, change.before) == ("qty", sum(qty))
    assert change.after == sum(q * w for q, w in zip(qty, weights, strict=True))
    assert any("размножает строки" in w and "qty" in w for w in report.warnings)


def test_duplicate_row_in_lookup_doubles_sums(con, tmp_path):
    stores = ["001", "002", "002", "003"]  # полный дубль строки справочника
    sales = ["001", "002", "003", "002", "001"]
    amounts = [10, 20, 30, 40, 50]
    report = relations_of(
        con,
        tmp_path,
        {
            "stores": {"store_id": stores, "city": ["A", "B", "B", "C"]},
            "sales": {"store_id": sales, "amount": ids(amounts)},
        },
    )
    (relation,) = report.relations
    assert relation.from_table == "sales"
    assert relation.confidence == "low"
    impact = relation.impact
    per_key = Counter(stores)
    assert impact.max_fanout == 2
    assert impact.rows_after == sum(per_key[s] for s in sales)
    (change,) = impact.changed_sums
    assert change.before == sum(amounts)
    assert change.after == sum(a * per_key[s] for s, a in zip(sales, amounts, strict=True))
    assert len(report.warnings) == 1


@pytest.mark.parametrize(
    ("known", "missing", "confidence"),
    [(20, 3, "medium"), (10, 3, "low"), (4, 6, None)],
)
def test_orphan_references_lower_confidence(con, tmp_path, known, missing, confidence):
    referenced = list(range(1, known + 1)) + list(range(100, 100 + missing))
    rows = referenced * 2
    report = relations_of(
        con,
        tmp_path,
        {
            "clients": {"client_id": ids(range(1, known + 1))},
            "orders": {"client_id": ids(rows), "amount": ids(range(len(rows)))},
        },
    )
    if confidence is None:  # покрытие 40% — ниже порога
        assert report.relations == ()
        return
    (relation,) = report.relations
    assert relation.overlap == round(known / (known + missing), 4)
    assert relation.confidence == confidence
    assert relation.impact.unmatched_rows == 2 * missing
    assert relation.impact.rows_after == relation.impact.rows_before == len(rows)
    assert any("без пары" in w for w in report.warnings)


def test_full_match_without_common_name_is_low_confidence(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "customers": {"customer_id": ids(range(1, 51))},
            "orders": {"buyer_id": ids(list(range(1, 51)) * 2)},
        },
    )
    (relation,) = report.relations
    assert (relation.from_column, relation.to_column) == ("buyer_id", "customer_id")
    assert (relation.name_match, relation.confidence) == (False, "low")


def test_id_column_named_id_matches_table_name(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "clients": {"id": ids(range(1, 11))},
            "orders": {"client_id": ids(list(range(1, 11)) * 3)},
        },
    )
    assert pairs(report) == {("orders", "client_id", "clients", "id")}
    assert report.relations[0].confidence == "high"


def test_non_key_columns_are_not_compared(con, tmp_path):
    dates = [f"0{d}.01.2024" for d in range(1, 10)]
    report = relations_of(
        con,
        tmp_path,
        {
            "a": {"day": dates, "amount": [f"{d},50 руб." for d in range(9)], "kind": ["x"] * 9},
            "b": {"day": dates, "amount": [f"{d},50 руб." for d in range(9)], "kind": ["x"] * 9},
        },
    )
    assert report.relations == ()


def test_relations_are_stored_and_replaced(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "clients": {"client_id": ids(range(1, 11))},
            "orders": {"client_id": ids(list(range(1, 11)) * 2)},
        },
    )
    assert read_relations(con) == report
    assert find_relations(con) == report
    assert con.execute(f"SELECT count(*) FROM {RELATIONS_TABLE}").fetchone()[0] == 1


def test_empty_dataset_has_no_relations(con):
    assert find_relations(con).relations == ()
    assert read_relations(con).relations == ()
