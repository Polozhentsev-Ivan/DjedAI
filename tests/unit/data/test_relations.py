"""Связи таблиц (B1-5) против эталонов, посчитанных независимо от DuckDB.

Таблицы собираются из Python-списков, ожидаемые меры пересечения, число строк и суммы
после JOIN считаются по тем же спискам через ``set`` и ``Counter``. Для синтетики PL-2
эталоны берутся из ``eval/synthetic_expectations.json``: их независимо пересчитывает
``scripts/verify_synthetic_data.py``.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from djedai.contracts.catalog import Column, DatasetCatalog, DatasetSource, Table
from djedai.data.csv_ingest import load_raw_csv, load_raw_csvs
from djedai.data.profiler import profile_tables, read_profiles
from djedai.data.relations import RELATIONS_TABLE, find_relations, read_relations
from djedai.data.typed_views import create_typed_views

SYNTHETIC = Path(__file__).parents[2] / "fixtures" / "synthetic"
EXPECTATIONS = json.loads(
    (Path(__file__).parents[3] / "eval" / "synthetic_expectations.json").read_text(encoding="utf-8")
)


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


@pytest.fixture
def synthetic(con):
    load_raw_csvs(con, sorted(SYNTHETIC.glob("*.csv")))
    create_typed_views(con)
    profile_tables(con)
    return find_relations(con)


def link(reference: dict) -> tuple[str, str, str, str]:
    source, target = reference["from"], reference["to"]
    return source["table"], source["column"], target["table"], target["column"]


def test_synthetic_relations_match_references(synthetic):
    """Эталоны из eval/ посчитаны по CSV независимо (scripts/verify_synthetic_data.py)."""
    references = EXPECTATIONS["relations"]
    assert pairs(synthetic) == {link(r) for r in references["expected"]}
    for relation, reference in zip(synthetic.relations, references["expected"], strict=True):
        assert (relation.from_table, relation.from_column) == link(reference)[:2]
        assert (relation.overlap, relation.jaccard, relation.cardinality) == (
            reference["overlap"],
            reference["jaccard"],
            reference["cardinality"],
        )
        assert relation.confidence == "high"
        join, impact = reference["join"], relation.impact
        assert (impact.rows_before, impact.rows_after, impact.unmatched_rows) == (
            join["rows_before"],
            join["rows_after"],
            join["unmatched_rows"],
        )
        amount = next(s for s in impact.sums if s.column == "total_amount")
        assert (amount.before, amount.after) == (
            join["total_amount_before"],
            join["total_amount_after"],
        )
    assert synthetic.warnings == ()


def test_synthetic_lookalike_ids_are_not_linked(synthetic):
    """sale_id 1..4168 и product_id 1..50 — похожие ID, но не связь; даты не сравниваются."""
    found = pairs(synthetic)
    for reference in EXPECTATIONS["relations"]["not_expected"]:
        source, column, target, target_column = link(reference)
        assert (source, column, target, target_column) not in found
        assert (target, target_column, source, column) not in found
    assert all("date" not in (r.from_column, r.to_column) for r in synthetic.relations)


def test_synthetic_catalog_shows_relations(con, synthetic):
    tables = [catalog_table(profile) for profile in read_profiles(con)]
    catalog = DatasetCatalog(
        dataset_id="ds_synthetic",
        name="Синтетика",
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
        source=DatasetSource(kind="csv_upload"),
        tables=tables,
        relations=[r.to_contract() for r in synthetic.relations],
        warnings=list(synthetic.warnings),
    )
    dumped = catalog.model_dump(mode="json")["relations"]
    assert dumped[1] == {
        "from": {"table": "sales", "column": "store_id"},
        "to": {"table": "stores", "column": "store_id"},
        "overlap": 1.0,
        "jaccard": 1.0,
        "cardinality": "N:1",
        "confidence": "high",
    }
    prompt = catalog.to_prompt()
    assert "- store_id: id, id, FK → stores.store_id" in prompt
    assert "- sales.product_id → products.product_id (N:1, уверенность high)" in prompt


ROLES = {"id": "id", "date": "time", "datetime": "time", "text": "text"}


def catalog_table(profile) -> Table:
    """Минимальная таблица каталога из профиля: сборщик каталога (B1-7) ещё не готов."""
    return Table(
        table_id=profile.view_name,
        raw_table=profile.raw_table,
        original_name=profile.view_name,
        row_count=profile.row_count,
        primary_key_candidates=list(profile.primary_key_candidates),
        time_columns=list(profile.time_columns),
        duplicate_rows=profile.duplicate_rows,
        columns=[
            Column(
                name=c.name,
                original_name=c.name,
                physical_type=c.physical_type,
                semantic_type=c.semantic_type,
                role=ROLES.get(c.semantic_type, "measure"),
                null_ratio=c.null_ratio,
                distinct_count=c.distinct_count,
                is_unique=c.is_unique,
            )
            for c in profile.columns
        ],
    )


def test_synthetic_relations_are_stored(con, synthetic):
    assert read_relations(con) == synthetic


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


@pytest.mark.parametrize("extra", [["x", "x", "y"], ["5", "6", "7"]])
def test_user_columns_named_like_service_aliases(con, tmp_path, extra):
    """Колонки ``k`` и ``n`` (текстовые и числовые) не путаются со служебными именами."""
    report = relations_of(
        con,
        tmp_path,
        {
            "stores": {"store_id": ["1", "2", "2"], "n": ["7", "8", "9"]},
            "sales": {"store_id": ["1", "1", "2"], "k": extra, "amount": ["10", "20", "30"]},
        },
    )
    (relation,) = report.relations
    impact = relation.impact
    assert (impact.rows_before, impact.rows_after, impact.max_fanout) == (3, 4, 2)
    amount = next(s for s in impact.sums if s.column == "amount")
    assert (amount.before, amount.after) == (60, 90)
    assert any("размножает строки" in w for w in report.warnings)


def test_null_keys_are_lost_by_inner_join(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "stores": {"store_id": ["1", "2"]},
            "sales": {"store_id": ["1", "1", "2", ""], "amount": ["10", "20", "30", "40"]},
        },
    )
    (relation,) = report.relations
    impact = relation.impact
    assert (impact.unmatched_rows, impact.null_key_rows) == (0, 1)
    assert (impact.rows_before, impact.rows_after, impact.rows_inner) == (4, 4, 3)
    (amount,) = impact.sums
    assert (amount.before, amount.after, amount.inner_after) == (100, 100, 60)
    (warning,) = report.warnings
    assert "с пустым ключом 1" in warning and "остаётся 3 из 4" in warning
    assert "amount 100 → 60" in warning


def test_orphans_and_null_keys_are_counted_separately(con, tmp_path):
    report = relations_of(
        con,
        tmp_path,
        {
            "clients": {"client_id": ids(range(1, 21))},
            "orders": {
                "client_id": ids(range(1, 21)) * 2 + ["99", "", ""],
                "amount": ids([1] * 43),
            },
        },
    )
    (relation,) = report.relations
    impact = relation.impact
    assert (impact.unmatched_rows, impact.null_key_rows, impact.rows_inner) == (1, 2, 40)
    (warning,) = report.warnings
    assert "без пары 1, с пустым ключом 2" in warning
    assert "amount 43 → 40" in warning


def test_view_suffix_does_not_change_entity_name(con, tmp_path):
    """При занятом имени представление называется clients_typed — связь та же."""
    con.execute("CREATE TABLE clients (x INTEGER)")
    report = relations_of(
        con,
        tmp_path,
        {
            "clients": {"id": ids(range(101, 201))},
            "orders": {"client_id": ids([101, 102] * 5)},
        },
    )
    assert pairs(report) == {("orders", "client_id", "clients_typed", "id")}
    assert report.relations[0].name_match
