"""Validate the final generated SQL before every analytical data read."""

from collections.abc import Collection
from typing import Any

import duckdb

from djedai.analytics.scope import parse_read_query, validate_dataset_query
from djedai.execution import current_budget


def check_budget() -> None:
    budget = current_budget()
    if budget is not None:
        budget.check()


def execute_dataset_query(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    parameters: list[Any] | None = None,
    *,
    allowed_tables: Collection[str] | None = None,
) -> duckdb.DuckDBPyConnection:
    check_budget()
    if allowed_tables is not None:
        validate_dataset_query(parse_read_query(query), allowed_tables)
    result = connection.execute(query, parameters)
    check_budget()
    return result


def check_dataset_table(table: str, allowed_tables: Collection[str] | None) -> None:
    """Check ownership scope before even trusted column-metadata queries run."""
    check_budget()
    if allowed_tables is not None:
        quoted = '"' + table.replace('"', '""') + '"'
        validate_dataset_query(parse_read_query(f"SELECT * FROM {quoted}"), allowed_tables)
