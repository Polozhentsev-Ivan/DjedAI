from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from djedai.data.csv_ingest import RawTable, load_raw_csv, read_sources
from djedai.data.typed_views import (
    TypedViewError,
    create_typed_view,
    create_typed_views,
    normalize_column_names,
    read_currencies,
    read_journal,
    read_typed_views,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "csv"


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def load(con, tmp_path, header: str, values: list[str], name: str = "t.csv") -> RawTable:
    """Файл с заданными значениями; пустая строка в списке — пропуск.

    Однокомпонентный заголовок дополняется колонкой ``k`` с разделителем ``;``: по одной
    колонке разделитель не определить, а запятые в числах должны остаться внутри поля.
    """
    path = tmp_path / name
    if ";" in header or "," in header:
        lines = [header, *values]
    else:
        lines = [f"{header};k", *(f'"{value}";x' for value in values)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_raw_csv(con, path)


def typed(con, tmp_path, values: list[str], header: str = "v"):
    raw = load(con, tmp_path, header, values)
    view = create_typed_view(con, raw)
    column = view.columns[0]
    out = [r[0] for r in con.execute(f'SELECT "{column.name}" FROM "{view.view_name}"').fetchall()]
    return view, column, out


def journal(con, action: str):
    return [e for e in read_journal(con) if e.action == action]


@pytest.mark.parametrize(
    ("values", "expected", "semantic"),
    [
        (
            ["1 234,56", "99,90", "0,00"],
            [Decimal("1234.56"), Decimal("99.90"), Decimal("0")],
            "float",
        ),
        (["1.5", "2.25", "-3"], [1.5, 2.25, -3.0], "float"),
        (["1 234", "-5", "7"], [1234, -5, 7], "integer"),
    ],
)
def test_numbers_become_numeric(con, tmp_path, values, expected, semantic):
    _, column, out = typed(con, tmp_path, values)
    assert column.semantic_type == semantic
    assert [float(x) for x in out] == [float(x) for x in expected]


@pytest.mark.parametrize(
    ("values", "currency", "expected"),
    [
        (["1 234,56 ₽", "99,90 ₽"], "RUB", [Decimal("1234.56"), Decimal("99.90")]),
        (["$99.90", "$1,000.00"], "USD", [Decimal("99.90"), Decimal("1000.00")]),
        (["100 EUR", "250 EUR"], "EUR", [Decimal("100"), Decimal("250")]),
    ],
)
def test_money_is_decimal_and_currency_is_journaled(con, tmp_path, values, currency, expected):
    _, column, out = typed(con, tmp_path, values)
    assert column.semantic_type == "money"
    assert column.physical_type.startswith("DECIMAL")
    assert [Decimal(x) for x in out] == expected
    assert journal(con, "parse_money")[0].detail["currency"] == currency


def test_percent_is_stored_as_number_without_division(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["12,5%", "7%", "100 %"])
    assert column.semantic_type == "percent"
    assert [float(x) for x in out] == [12.5, 7.0, 100.0]


@pytest.mark.parametrize(
    ("values", "fmt"),
    [
        (["25.03.2026", "01.12.2025"], "%d.%m.%Y"),
        (["2026-03-25", "2025-12-01"], "%Y-%m-%d"),
        (["2026/03/25", "2025/12/01"], "%Y/%m/%d"),
        (["25/03/2026", "01/12/2025"], "%d/%m/%Y"),
        (["03/25/2026", "12/01/2025"], "%m/%d/%Y"),
    ],
)
def test_dates_use_one_format_per_column(con, tmp_path, values, fmt):
    _, column, out = typed(con, tmp_path, values)
    assert column.semantic_type == "date"
    assert column.format == fmt
    assert len(out) == 2
    assert all(isinstance(x, date) for x in out)
    assert {x.month for x in out} == {3, 12}


def test_datetime(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["25.03.2026 14:30:00", "01.12.2025 08:05:09"])
    assert column.semantic_type == "datetime"
    assert out[0] == datetime(2026, 3, 25, 14, 30)


def test_ambiguous_dates_stay_text(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["03/04/2026", "05/06/2026"])
    assert column.semantic_type == "text"
    assert out == ["03/04/2026", "05/06/2026"]
    assert journal(con, "keep_text")[0].detail["reason"] == "ambiguous_date"


def test_bool(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["да", "нет", "true", "False"])
    assert column.semantic_type == "bool"
    assert out == [True, False, True, False]


def test_leading_zero_ids_are_not_converted(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["00123", "00124", "00125"], header="order_id")
    assert column.semantic_type == "id"
    assert column.physical_type == "VARCHAR"
    assert out == ["00123", "00124", "00125"]


def test_integer_id_column_by_name_stays_text(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["1", "2", "3"], header="customer_id")
    assert column.semantic_type == "id"
    assert out == ["1", "2", "3"]


def test_mixed_currencies_stay_text(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["100 ₽", "$5.00", "7 €"])
    assert column.physical_type == "VARCHAR"
    assert out == ["100 ₽", "$5.00", "7 €"]
    entry = journal(con, "keep_text")[0]
    assert entry.detail["reason"] == "mixed_currencies"
    assert set(entry.detail["currencies"]) == {"RUB", "USD", "EUR"}


def test_unparsed_value_is_journaled_and_original_kept(con, tmp_path):
    values = [f"{n},50" for n in range(1, 40)] + ["abc"]
    raw = load(con, tmp_path, "amount", values)
    view = create_typed_view(con, raw)
    assert view.columns[0].semantic_type == "float"
    typed_values = con.execute(f'SELECT amount FROM "{view.view_name}"').fetchall()
    assert typed_values[-1] == (None,)
    assert con.execute(f'SELECT amount FROM "{raw.raw_table}"').fetchall()[-1] == ("abc",)
    entry = journal(con, "unparsed_values")[0]
    assert entry.detail["count"] == 1
    assert entry.detail["examples"] == ["abc"]


def test_mostly_unparseable_column_stays_text(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["1,5", "x", "y", "z"])
    assert column.physical_type == "VARCHAR"
    assert out == ["1,5", "x", "y", "z"]
    assert journal(con, "keep_text")[0].detail["reason"] == "text"


def test_empty_and_null_values(con, tmp_path):
    raw = load(con, tmp_path, "a,b", ["1,", "2,", "3,"])
    view = create_typed_view(con, raw)
    assert [c.semantic_type for c in view.columns] == ["integer", "text"]
    assert journal(con, "keep_text")[0].detail["reason"] == "empty_column"


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (["Сумма заказа", "Order ID"], ("summa_zakaza", "order_id")),
        (["a", "A", "a"], ("a", "a_2", "a_3")),
        (["", " "], ("column1", "column2")),
        (["2026"], ("col_2026",)),
        (["Цена, ₽"], ("tsena",)),
    ],
)
def test_normalize_column_names(columns, expected):
    assert normalize_column_names(columns) == expected


def test_registry_maps_normalized_names_to_originals(con, tmp_path):
    raw = load(con, tmp_path, "Сумма заказа;Order ID", ["1;a", "2;b"])
    view = create_typed_view(con, raw)
    rows = con.execute(
        "SELECT original_name, normalized_name FROM _djedai_columns ORDER BY position"
    ).fetchall()
    assert rows == [("Сумма заказа", "summa_zakaza"), ("Order ID", "order_id")]
    assert [c.name for c in view.columns] == ["summa_zakaza", "order_id"]
    assert journal(con, "rename_column")[0].detail == {"from": "Сумма заказа", "to": "summa_zakaza"}


def test_orders_fixture_raw_and_typed(con):
    raw = load_raw_csv(con, FIXTURES / "orders_cp1251_semicolon.csv")
    view = create_typed_view(con, raw)
    by_name = {c.name: c for c in view.columns}
    assert by_name["order_id"].semantic_type == "id"
    assert by_name["amount"].semantic_type == "float"
    raw_ids = con.execute(f'SELECT order_id FROM "{raw.raw_table}"').fetchall()
    typed_ids = con.execute(f'SELECT order_id FROM "{view.view_name}"').fetchall()
    assert raw_ids == typed_ids
    assert ("00123",) in typed_ids


def test_raw_table_is_unchanged(con, tmp_path):
    raw = load(con, tmp_path, "v", ["1 234,56 ₽", "99,90 ₽", "oops"])
    before = con.execute(f'SELECT * FROM "{raw.raw_table}"').fetchall()
    create_typed_view(con, raw)
    assert con.execute(f'SELECT * FROM "{raw.raw_table}"').fetchall() == before


def test_create_is_idempotent(con, tmp_path):
    raw = load(con, tmp_path, "Сумма", ["1,5", "2,5"])
    create_typed_view(con, raw)
    first = [(e.action, e.detail) for e in read_journal(con)]
    create_typed_view(con, raw)
    assert [(e.action, e.detail) for e in read_journal(con)] == first
    assert con.execute("SELECT count(*) FROM _djedai_columns").fetchone() == (2,)


def test_view_name_has_no_raw_prefix_and_avoids_collision(con, tmp_path):
    con.execute('CREATE TABLE "t" (x INTEGER)')
    raw = load(con, tmp_path, "v", ["1", "2"])
    assert raw.raw_table == "raw_t"
    view = create_typed_view(con, raw)
    assert view.view_name == "t_typed"


def test_failure_leaves_no_view_or_journal(con, tmp_path, monkeypatch):
    raw = load(con, tmp_path, "v", ["1", "2"])

    def boom(*args, **kwargs):
        raise duckdb.Error("boom")

    monkeypatch.setattr("djedai.data.typed_views._write_registry", boom)
    with pytest.raises(TypedViewError, match="raw_t"):
        create_typed_view(con, raw)
    views = con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()
    assert views == []
    assert read_journal(con) == []


def test_create_typed_views_for_all_sources(con):
    load_raw_csv(con, FIXTURES / "orders_utf8_comma.csv")
    load_raw_csv(con, FIXTURES / "customers_utf8_semicolon.csv")
    report = create_typed_views(con)
    assert report.errors == ()
    assert {v.raw_table for v in report.views} == {r.raw_table for r in read_sources(con)}


def test_european_thousands_keep_fraction(con, tmp_path):
    _, column, out = typed(con, tmp_path, ["1.234,56", "2.345,67"])
    assert column.semantic_type == "float"
    assert [float(x) for x in out] == [1234.56, 2345.67]
    assert journal(con, "parse_number")[0].detail["decimal_separator"] == ","
    assert journal(con, "parse_integer") == []


def test_existing_view_is_not_overwritten(con, tmp_path):
    con.execute('CREATE VIEW "t" AS SELECT 999 AS sentinel')
    raw = load(con, tmp_path, "v", ["10", "20"])
    view = create_typed_view(con, raw)
    assert view.view_name == "t_typed"
    assert con.execute('SELECT sentinel FROM "t"').fetchall() == [(999,)]
    assert con.execute('SELECT v FROM "t_typed"').fetchall() == [(10,), (20,)]


def test_read_typed_views_matches_created_views(con):
    load_raw_csv(con, FIXTURES / "orders_utf8_comma.csv")
    load_raw_csv(con, FIXTURES / "customers_utf8_semicolon.csv")
    report = create_typed_views(con)
    expected = sorted(report.views, key=lambda v: v.raw_table)
    assert read_typed_views(con) == expected
    assert all(len(v.columns) > 1 for v in expected)  # порядок колонок проверяется на деле


def test_read_typed_views_empty_registry(con):
    assert read_typed_views(con) == []


def test_recreating_keeps_own_view_name_next_to_foreign_view(con, tmp_path):
    con.execute('CREATE VIEW "t" AS SELECT 999 AS sentinel')
    raw = load(con, tmp_path, "v", ["10", "20"])
    first = create_typed_view(con, raw)
    second = create_typed_view(con, raw)
    assert first.view_name == second.view_name == "t_typed"
    assert con.execute('SELECT sentinel FROM "t"').fetchall() == [(999,)]


def test_currency_is_read_for_money_columns_only(con, tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "Сумма;Цена;Скидка;Количество\n1 234,56 ₽;$10.50;5%;3\n99,90 ₽;$20.00;7%;4\n",
        encoding="utf-8",
    )
    view = create_typed_view(con, load_raw_csv(con, path))

    assert read_currencies(con) == {
        (view.view_name, "summa"): "RUB",
        (view.view_name, "tsena"): "USD",
    }  # проценты и целые числа без валюты в результат не попадают


def test_currency_is_not_read_for_mixed_or_unknown_currencies(con, tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text(
        "Смешанная;Без валюты\n100 ₽;100,5\n200 $;300,5\n300 ₽;400,5\n", encoding="utf-8"
    )
    create_typed_view(con, load_raw_csv(con, path))
    assert read_currencies(con) == {}


def test_currency_is_filtered_by_raw_table(con, tmp_path):
    first = load(con, tmp_path, "v", ["100 EUR", "250 EUR"], name="a.csv")
    second = load(con, tmp_path, "v", ["1 ₽", "2 ₽"], name="b.csv")
    first_view = create_typed_view(con, first)
    second_view = create_typed_view(con, second)

    assert read_currencies(con, first.raw_table) == {(first_view.view_name, "v"): "EUR"}
    assert read_currencies(con, second.raw_table) == {(second_view.view_name, "v"): "RUB"}
    assert len(read_currencies(con)) == 2


def test_currency_is_replaced_when_view_is_rebuilt(con, tmp_path):
    raw = load(con, tmp_path, "v", ["100 EUR", "250 EUR"])
    view = create_typed_view(con, raw)
    create_typed_view(con, raw)  # повторный вызов заменяет записи журнала
    assert read_currencies(con) == {(view.view_name, "v"): "EUR"}


def test_percent_values_without_sign_are_journaled(con, tmp_path):
    _, column, _ = typed(con, tmp_path, ["15%", "20%", "0,15"])
    assert column.semantic_type == "percent"
    assert journal(con, "parse_percent")[0].detail["without_percent_sign"] == 1


def test_percent_with_sign_everywhere_has_no_scale_note(con, tmp_path):
    typed(con, tmp_path, ["15%", "20%"])
    assert "without_percent_sign" not in journal(con, "parse_percent")[0].detail


def test_date_format_chosen_by_majority_journals_ambiguous_values(con, tmp_path):
    _, column, _ = typed(con, tmp_path, ["25/03/2026", "03/04/2026", "05/06/2026"])
    assert column.format == "%d/%m/%Y"
    detail = journal(con, "parse_date")[0].detail
    assert detail["ambiguous_values"] == 2
    assert detail["alternative_formats"] == ["%m/%d/%Y"]


def test_unambiguous_dates_have_no_ambiguity_note(con, tmp_path):
    typed(con, tmp_path, ["25.03.2026", "01.12.2025"])
    assert "ambiguous_values" not in journal(con, "parse_date")[0].detail
