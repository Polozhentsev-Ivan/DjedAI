from pathlib import Path

import duckdb

from djedai.contracts.catalog import DatasetCatalog
from djedai.data.catalog_builder import build_csv_catalog
from djedai.data.csv_ingest import load_raw_csv
from djedai.data.profiler import profile_table
from djedai.data.typed_views import create_typed_view


def test_catalog_preserves_profile_currency_original_names_and_journal(tmp_path: Path):
    source = tmp_path / "orders.csv"
    source.write_text(
        "id;Сумма;Дата;Город;Активен\n"
        "001;1 200,50 ₽;01.01.2024;Москва;да\n"
        "002;;02.01.2024;Казань;нет\n"
        "001;1 200,50 ₽;01.01.2024;Москва;да\n",
        encoding="utf-8",
    )
    with duckdb.connect() as connection:
        raw = load_raw_csv(connection, source, header=True)
        view = create_typed_view(connection, raw)
        profile = profile_table(connection, view)
        catalog = build_csv_catalog(connection, raw, view, profile, dataset_id="ds_currency")
    table = catalog.tables[0]
    assert table.row_count == 3
    assert table.duplicate_rows == 1
    amount = table.column("summa")
    assert amount.original_name == "Сумма"
    assert amount.semantic_type == "money"
    assert amount.currency == "RUB"
    assert amount.role == "measure"
    assert amount.null_ratio == 1 / 3
    assert table.column("data").role == "time"
    assert table.column("id").role == "id"
    assert table.column("gorod").role == "text"
    assert table.column("aktiven").role == "dimension"
    assert any(entry.action == "parse_money" for entry in catalog.transformations)
    assert "summa: money" in catalog.to_prompt()
    assert DatasetCatalog.model_validate_json(catalog.model_dump_json()) == catalog


def test_low_cardinality_text_is_a_dimension_and_text_code_an_id(tmp_path: Path):
    source = tmp_path / "sales.csv"
    source.write_text(
        "Код заказа;Канал продажи;Комментарий\n"
        "ПР-ЗК-0000001;Розница;первый\n"
        "ПР-ЗК-0000001;Интернет;второй\n"
        "ПР-ЗК-0000002;Розница;третий\n"
        "ПР-ЗК-0000003;Розница;четвёртый\n",
        encoding="utf-8",
    )
    with duckdb.connect() as connection:
        raw = load_raw_csv(connection, source, header=True)
        view = create_typed_view(connection, raw)
        profile = profile_table(connection, view)
        table = build_csv_catalog(connection, raw, view, profile, dataset_id="ds_sales").tables[0]
    roles = {c.name: (c.semantic_type, c.role) for c in table.columns}
    assert roles == {
        "kod_zakaza": ("id", "id"),
        "kanal_prodazhi": ("category", "dimension"),
        "kommentariy": ("text", "text"),
    }
