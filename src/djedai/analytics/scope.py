"""Server-side SQL scope for the selected dataset, independent of model input."""

import re
from collections.abc import Collection

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

_DOLLAR_TAG = re.compile(r"\$\w*\$")
_WORD = re.compile(r"[\w$]+")


class DatasetScopeError(ValueError):
    """The query requests a source outside the selected catalog."""


def _skip_quoted(sql: str, start: int) -> int:
    """End of the literal, quoted identifier or comment at ``start``, or ``start`` itself."""
    char = sql[start]
    if char == "'" or char == '"':
        # E'...' strings also escape with a backslash; doubling works everywhere.
        escapes = char == "'" and start > 0 and sql[start - 1] in "eE"
        index = start + 1
        while index < len(sql):
            if escapes and sql[index] == "\\":
                index += 2
            elif sql[index] != char:
                index += 1
            elif sql[index + 1 : index + 2] == char:
                index += 2
            else:
                return index + 1
        return len(sql)
    if sql.startswith("--", start):
        end = sql.find("\n", start)
        return len(sql) if end == -1 else end
    if sql.startswith("/*", start):
        depth, index = 1, start + 2
        while index < len(sql) and depth:
            if sql.startswith("/*", index):
                depth, index = depth + 1, index + 2
            elif sql.startswith("*/", index):
                depth, index = depth - 1, index + 2
            else:
                index += 1
        return index
    if char == "$" and (tag := _DOLLAR_TAG.match(sql, start)):
        end = sql.find(tag.group(), tag.end())
        return len(sql) if end == -1 else end + len(tag.group())
    return start


def quote_catalog_tables(sql: str, allowed_tables: Collection[str]) -> str:
    """Quote catalog tables whose names cannot be written bare (``08_jobs``).

    Literals, comments and quoted identifiers stay intact. Only exact names of the
    selected dataset change, and the result still passes the usual scope validation,
    so this cannot widen access.
    """
    names = {
        name.casefold(): name
        for name in allowed_tables
        if name[:1].isdigit() and any(char.isalpha() for char in name)
    }
    if not names:
        return sql
    parts: list[str] = []
    index = 0
    while index < len(sql):
        end = _skip_quoted(sql, index)
        if end > index:
            parts.append(sql[index:end])
            index = end
        elif word := _WORD.match(sql, index):
            name = names.get(word.group().casefold())
            parts.append(word.group() if name is None else '"' + name.replace('"', '""') + '"')
            index = word.end()
        else:
            parts.append(sql[index])
            index += 1
    return "".join(parts)


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
