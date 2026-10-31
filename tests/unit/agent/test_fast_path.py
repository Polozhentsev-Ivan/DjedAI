from pathlib import Path

import pytest

from djedai.agent.fast_path import (
    FastPathError,
    classify_fast_query,
    count_from_result,
    count_markdown,
    count_sql,
    structure_markdown,
)
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ToolError, ToolResult, ToolResultData

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples" / "dataset_catalog.json"
)


@pytest.fixture
def catalog() -> DatasetCatalog:
    return DatasetCatalog.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def one_table(catalog: DatasetCatalog) -> DatasetCatalog:
    return catalog.model_copy(update={"tables": [catalog.tables[0]], "relations": []})


@pytest.fixture
def prefixed_table_names(catalog: DatasetCatalog) -> DatasetCatalog:
    """`orders.csv` and `table orders.csv` must stay distinct."""
    base = catalog.tables[0]
    plain = base.model_copy(
        update={"table_id": "orders.csv", "original_name": "orders.csv", "row_count": 2}
    )
    prefixed = base.model_copy(
        update={
            "table_id": "table orders.csv",
            "original_name": "table orders.csv",
            "row_count": 5,
        }
    )
    return catalog.model_copy(update={"tables": [plain, prefixed], "relations": []})


@pytest.mark.parametrize(
    ("question", "kind", "table_id"),
    [
        ("Сколько строк в таблице orders?", "row_count", "orders"),
        ("Сколько всего строк в таблице «Заказы_2024.csv»?", "row_count", "orders"),
        ("Сколько записей в clients?", "row_count", "clients"),
        ("Какое количество строк в таблице clients?", "row_count", "clients"),
        ("How many rows in table clients?", "row_count", "clients"),
        ("What is the row count of table clients?", "row_count", "clients"),
        ("Какова структура таблицы orders?", "structure", "orders"),
        ("Покажи структуру таблицы clients, пожалуйста?", "structure", "clients"),
        ("Структура clients?", "structure", "clients"),
        ("Какие колонки в таблице orders?", "structure", "orders"),
        ("Покажи схему «Заказы_2024.csv»", "structure", "orders"),
        ("What columns are in table clients?", "structure", "clients"),
        ("Show structure of table orders", "structure", "orders"),
    ],
)
def test_supported_queries_are_recognized(catalog, question, kind, table_id):
    path = classify_fast_query(question, catalog)

    assert path is not None
    assert (path.kind, path.table_id) == (kind, table_id)


@pytest.mark.parametrize(
    "question",
    [
        "Сколько строк?",
        "Сколько строк в таблице?",
        "Покажи структуру",
        "Какие колонки?",
        "Сколько строк в таблице missing?",
        "Сколько строк в orders и clients?",
        "Сколько строк в orders после 2024 года?",
        "Сколько строк в orders по датам?",
        "Сколько уникальных клиентов в orders?",
        "Сколько строк в orders JOIN clients?",
        "Сколько строк в orders, сгруппируй по месяцам?",
        "Сколько строк в orders, исключая отменённые?",
        "Сколько строк в orders не поступило?",
        "Сколько строк было в прошлом ответе?",
        "А сколько строк в orders?",
        "Сколько строк и какова сумма?",
        "Сколько строк в orders в валюте USD?",
        "Покажи структуру orders с аномалиями",
        "Опиши, сколько строк в orders",
        "Какие колонки в orders содержат пропуски?",
        "How many rows in orders after 2024?",
        "How many unique customers are in orders?",
    ],
)
def test_negative_questions_stay_on_the_general_route(catalog, question):
    assert classify_fast_query(question, catalog) is None


@pytest.mark.parametrize("opening,closing", [("'", "'"), ("«", "»"), ("“", "”"), ("‘", "’")])
def test_quoted_table_name_keeps_its_table_prefix(prefixed_table_names, opening, closing):
    token = f"{opening}table orders.csv{closing}"

    count = classify_fast_query(f"How many rows are in the table {token}?", prefixed_table_names)
    structure = classify_fast_query(f"Show structure of table {token}", prefixed_table_names)

    assert (count.kind, count.table_id) == ("row_count", "table orders.csv")
    assert (structure.kind, structure.table_id) == ("structure", "table orders.csv")


def test_unquoted_exact_name_wins_before_table_prefix_fallback(prefixed_table_names):
    path = classify_fast_query("Сколько строк в таблице table orders.csv?", prefixed_table_names)

    assert (path.kind, path.table_id) == ("row_count", "table orders.csv")


def test_unquoted_table_prefix_fallback_stays_available(prefixed_table_names):
    plain_only = prefixed_table_names.model_copy(
        update={"tables": [prefixed_table_names.tables[0]], "relations": []}
    )

    path = classify_fast_query("Покажи структуру таблицы table orders.csv", plain_only)

    assert (path.kind, path.table_id) == ("structure", "orders.csv")


def test_quoted_name_is_literal_and_does_not_fall_back_to_prefix_stripping(
    prefixed_table_names,
):
    plain_only = prefixed_table_names.model_copy(
        update={"tables": [prefixed_table_names.tables[0]], "relations": []}
    )

    assert classify_fast_query("Сколько строк в таблице ‘table orders.csv’?", plain_only) is None


def test_single_table_allows_omitting_its_name(one_table):
    assert classify_fast_query("Сколько строк?", one_table).table_id == "orders"
    assert classify_fast_query("Покажи структуру таблицы", one_table).table_id == "orders"
    assert classify_fast_query("Какие колонки?", one_table).table_id == "orders"


def test_count_sql_quotes_the_verified_table_and_counts_all_rows():
    assert count_sql('orders"2024') == 'SELECT COUNT(*) AS row_count FROM "orders""2024"'


@pytest.mark.parametrize("value", [0, 1, 125430])
def test_count_is_read_from_the_result_cell(value):
    result = ToolResult(
        result_id="r_count",
        status="ok",
        data=ToolResultData(columns=["row_count"], rows=[[value]]),
    )

    assert count_from_result(result) == value
    assert count_markdown("orders", value) == f"Строк в таблице `orders`: {value}."


def test_null_and_empty_count_results_are_not_zero():
    null = ToolResult(
        result_id="r_null",
        status="ok",
        data=ToolResultData(columns=["row_count"], rows=[[None]]),
    )
    empty = ToolResult(
        result_id="r_empty",
        status="empty",
        data=ToolResultData(columns=["row_count"], rows=[]),
    )

    with pytest.raises(FastPathError, match="NULL"):
        count_from_result(null)
    with pytest.raises(FastPathError, match="не вернул число"):
        count_from_result(empty)


def test_tool_error_is_reported_verbatim_without_fallback_count():
    result = ToolResult(
        result_id="r_error",
        status="error",
        error=ToolError(
            code="database_error", message="Таблица недоступна", suggestion="Повторите"
        ),
    )

    with pytest.raises(FastPathError) as error:
        count_from_result(result)

    assert error.value.message == "Таблица недоступна"
    assert error.value.suggestion == "Повторите"


def test_structure_comes_from_the_current_catalog_without_profile_row_count(catalog):
    markdown = structure_markdown(catalog, "orders")

    assert "Структура таблицы `orders`" in markdown
    assert "`order_id`" in markdown
    assert "datetime" in markdown
    assert "ревизия 2" in markdown
    # row_count профиля не выдаётся как свежий COUNT
    assert "125430" not in markdown
