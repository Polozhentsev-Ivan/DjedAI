"""Server-side SQL scope for the selected dataset, independent of model input."""

from collections.abc import Collection

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


class DatasetScopeError(ValueError):
    """The query requests a source outside the selected catalog."""


def parse_read_query(sql: str) -> exp.Expression:
    """Parse exactly one read query; parse_one would silently discard later statements."""
    statements = sqlglot.parse(sql, read="duckdb")
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise DatasetScopeError("Разрешён один запрос SELECT к выбранному датасету")
    return statements[0]


def validate_dataset_query(query: exp.Expression, allowed_tables: Collection[str]) -> None:
    """Resolve CTE/subquery aliases, then allow only local catalog views.

    Attached PostgreSQL connections are deliberately not queryable by model SQL.
    Trusted views may read them; arbitrary table functions and user-defined macros
    could bypass that boundary and are therefore rejected before execution.
    """
    allowed = {name.casefold() for name in allowed_tables}
    for scope in traverse_scope(query):
        for _, source in scope.selected_sources.values():
            if isinstance(source, Scope):
                continue
            if (
                not isinstance(source, exp.Table)
                or not isinstance(source.this, exp.Identifier)
                or source.catalog
                or (source.db and source.db.casefold() != "main")
                or source.name.casefold() not in allowed
            ):
                raise DatasetScopeError("Доступны только таблицы каталога выбранного датасета")

    for function in query.find_all(exp.Func):
        name = function.name if isinstance(function, exp.Anonymous) else function.sql_name()
        name = name.casefold()
        # Unknown functions may be persisted macros with arbitrary table access.
        # SQLGlot knows the standard analytic/scalar functions used by this app.
        if (
            isinstance(function, exp.Anonymous)
            or name.startswith(("read_", "scan_", "postgres_", "duckdb_", "sqlite_"))
            or name
            in {
                "query",
                "query_table",
                "current_query",
                "current_setting",
                "getenv",
                "getvariable",
                "glob",
                "http_get",
                "http_post",
                "sniff_csv",
                "which_secret",
            }
            or isinstance(function.parent, exp.Dot)
        ):
            raise DatasetScopeError("Функция SQL недоступна в области выбранного датасета")
