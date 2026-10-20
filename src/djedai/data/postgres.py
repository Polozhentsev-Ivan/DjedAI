"""PostgreSQL как источник только для чтения (B1-8).

Данные не выгружаются: БД подключается через ``ATTACH ... (TYPE postgres, READ_ONLY)``,
а над выбранными таблицами строятся представления DuckDB, которые читают живые данные.
Для каждой таблицы — два представления, как у CSV: «сырое» ``pg_<схема>_<таблица>``
(``SELECT *`` из исходной таблицы) и типизированное ``<таблица>`` с именами колонок в
snake_case. Типы берутся из БД; колонки записываются в реестр ``_djedai_columns`` и журнал
``typed_views``, поэтому общий профайлер и поиск связей работают без изменений.

Запись запрещена тремя слоями: ``READ_ONLY`` у ATTACH (DuckDB отклоняет DML и DDL), сессия
PostgreSQL с ``default_transaction_read_only=on`` (проверяется после подключения) и
read-only учётная запись на стороне БД (``infra/postgres/roles.sql``). Гарантию даёт только
последняя: права роли — единственное, что не зависит от клиента.

Пароль передаётся только во временном секрете DuckDB: в строке ATTACH, в
``duckdb_databases()``, ``duckdb_secrets()`` и в сообщениях об ошибках его нет. ATTACH и
секрет не сохраняются в файле DuckDB: после открытия датасета подключение повторяют.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

import duckdb

from djedai.data.csv_ingest import slugify
from djedai.data.typed_views import (
    _ID_NAME,
    TypedColumn,
    TypedView,
    _ensure_tables,
    _ident,
    _lit,
    _Plan,
    _rename_entries,
    _view_name,
    _write_registry,
    normalize_column_names,
)

ALIAS = "pg_src"
SOURCES_TABLE = "_djedai_db_sources"
SOURCE_KIND = "postgres"
DEFAULT_PORT = 5432
CONNECT_TIMEOUT = 10
MAX_CONNECT_TIMEOUT = 300
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
# Остальные параметры libpq не принимаются: options мог бы отключить read-only,
# а passfile, service, sslkey и подобные читают файлы на сервере DjedAI.
DSN_KEYS = (
    "host", "port", "dbname", "user", "password", "sslmode", "connect_timeout", "application_name",
)  # fmt: skip
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
INTEGER_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
)  # fmt: skip
FLOAT_TYPES = ("FLOAT", "DOUBLE")
_KEYWORD = re.compile(r"(\w+)\s*=\s*(?:'((?:[^'\\]|\\.)*)'|([^\s']*))\s*")


class PostgresError(Exception):
    """Подключиться или прочитать таблицу не удалось; ``str(error)`` можно показывать.

    Пароль в сообщение не попадает. ``table`` — ``схема.таблица`` при ошибке одной таблицы.
    """

    def __init__(self, reason: str, table: str | None = None) -> None:
        super().__init__(f"Таблица «{table}»: {reason}" if table else f"PostgreSQL: {reason}")
        self.reason = reason
        self.table = table


@dataclass(frozen=True)
class PgConnectionInfo:  # разобранная строка подключения; пароль не попадает в repr
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    sslmode: str | None = None
    connect_timeout: int = CONNECT_TIMEOUT
    application_name: str | None = None

    def describe(self) -> str:
        """Адрес источника без пароля: ``user@host:port/dbname``."""
        return f"{self.user}@{self.host}:{self.port}/{self.dbname}"

    def libpq(self, *, read_only: bool = True) -> str:
        """Строка ATTACH без пароля; по умолчанию сессия PostgreSQL только на чтение.

        ``read_only=False`` — только для загрузки демо-данных (``scripts/seed_demo_postgres.py``).
        """
        params = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.dbname,
            "user": self.user,
            "sslmode": self.sslmode,
            "connect_timeout": str(self.connect_timeout),
            "application_name": self.application_name,
            "options": READ_ONLY_OPTIONS if read_only else None,
        }
        return " ".join(f"{k}={_quote(v)}" for k, v in params.items() if v is not None)


@dataclass(frozen=True)
class DbSource:  # исходная таблица подключённой БД для представления
    raw_table: str
    view_name: str
    source_kind: str
    relation: str  # схема.таблица в исходной БД


@dataclass(frozen=True)
class PostgresReport:  # итог подключения: источник, построенные представления, ошибки таблиц
    source: str
    views: tuple[TypedView, ...]
    errors: tuple[PostgresError, ...]


def parse_dsn(dsn: str) -> PgConnectionInfo:
    """Разбирает URI ``postgresql://user:password@host:port/db?sslmode=...`` или ``key=value``.

    Пользователь и пароль обязательны: без пароля libpq взял бы его из окружения или
    ``.pgpass`` сервера DjedAI. Принимаются только параметры из ``DSN_KEYS``.
    """
    text = dsn.strip()
    if not text:
        raise PostgresError("строка подключения пуста")
    params = _parse_uri(text) if re.match(r"postgres(?:ql)?://", text) else _parse_keywords(text)
    unknown = sorted(set(params) - set(DSN_KEYS))
    if unknown:
        raise PostgresError(f"параметры не поддерживаются: {', '.join(unknown)}")
    user, password = params.get("user"), params.get("password")
    if not user:
        raise PostgresError("не указан пользователь")
    if not password:
        raise PostgresError("не указан пароль")
    sslmode = params.get("sslmode") or None
    if sslmode is not None and sslmode not in SSL_MODES:
        raise PostgresError(f"sslmode должен быть одним из: {', '.join(SSL_MODES)}")
    return PgConnectionInfo(
        host=params.get("host") or "localhost",
        port=_number(params.get("port"), DEFAULT_PORT, "port", 1, 65535),
        dbname=params.get("dbname") or user,
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=_number(
            params.get("connect_timeout"),
            CONNECT_TIMEOUT,
            "connect_timeout",
            1,
            MAX_CONNECT_TIMEOUT,
        ),  # fmt: skip
        application_name=params.get("application_name") or None,
    )


def attach_postgres(
    connection: duckdb.DuckDBPyConnection,
    dsn: str | PgConnectionInfo,
    alias: str = ALIAS,
) -> PgConnectionInfo:
    """Подключает БД как ``alias`` только на чтение; прежнее подключение с тем же именем
    заменяется. Бросает ``PostgresError``, если подключиться не удалось или сессия
    PostgreSQL допускает запись."""
    info = parse_dsn(dsn) if isinstance(dsn, str) else dsn
    _load_extension(connection)
    secret = _ident(f"{alias}_secret")
    detach_postgres(connection, alias)
    try:
        connection.execute(
            f"CREATE TEMPORARY SECRET {secret} (TYPE postgres, PASSWORD {_lit(info.password)})"
        )
        connection.execute(
            f"ATTACH {_lit(info.libpq())} AS {_ident(alias)} "
            f"(TYPE postgres, SECRET {secret}, READ_ONLY)"
        )
        read_only = connection.execute(
            f"SELECT * FROM postgres_query({_lit(alias)}, "
            "'SELECT current_setting(''transaction_read_only'')')"
        ).fetchone()[0]
    except duckdb.Error as error:
        detach_postgres(connection, alias)
        # исходное исключение не сцепляем: текст libpq уходит в логи вместе с traceback
        raise PostgresError(_explain(str(error), info)) from None
    if read_only != "on":
        detach_postgres(connection, alias)
        raise PostgresError("сервер не перевёл сессию в режим только для чтения")
    return info


def detach_postgres(connection: duckdb.DuckDBPyConnection, alias: str = ALIAS) -> None:
    """Отключает БД и удаляет секрет с паролем; без подключения ничего не делает."""
    connection.execute(f"DETACH DATABASE IF EXISTS {_ident(alias)}")
    connection.execute(f"DROP TEMPORARY SECRET IF EXISTS {_ident(f'{alias}_secret')}")


def list_tables(connection: duckdb.DuckDBPyConnection, alias: str = ALIAS) -> list[tuple[str, str]]:
    """Таблицы и представления подключённой БД ``(схема, имя)`` без системных схем."""
    system = ", ".join(_lit(schema) for schema in SYSTEM_SCHEMAS)
    return connection.execute(
        f"SELECT schema_name, table_name FROM duckdb_tables() "
        f"WHERE database_name = $alias AND schema_name NOT IN ({system}) "
        f"UNION SELECT schema_name, view_name FROM duckdb_views() "
        f"WHERE database_name = $alias AND NOT internal AND schema_name NOT IN ({system}) "
        f"ORDER BY 1, 2",
        {"alias": alias},
    ).fetchall()


def select_tables(
    available: Sequence[tuple[str, str]], requested: Sequence[str] | None
) -> list[tuple[str, str]]:
    """Выбранные таблицы: ``схема.таблица`` или имя в ``public``; ``None`` — все.

    Неизвестные имена дают ``PostgresError`` со списком; повторы убираются.
    """
    if requested is None:
        return list(available)
    known = set(available)
    selected: list[tuple[str, str]] = []
    missing: list[str] = []
    for name in requested:
        schema, _, table = name.rpartition(".")
        key = (schema or "public", table)
        if key not in known:
            missing.append(name)
        elif key not in selected:
            selected.append(key)
    if missing:
        raise PostgresError(f"таблицы не найдены: {', '.join(missing)}")
    if not selected:
        raise PostgresError("не выбрано ни одной таблицы")
    return selected


def create_postgres_view(
    connection: duckdb.DuckDBPyConnection, schema: str, table: str, alias: str = ALIAS
) -> TypedView:
    """Строит представления над ``alias.schema.table`` и записывает реестр; операция атомарна.

    Повторный вызов для той же таблицы пересоздаёт представления под прежними именами.
    """
    relation = f"{schema}.{table}"
    try:
        _ensure_tables(connection)
        _ensure_sources(connection)
        columns = connection.execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ? "
            "ORDER BY column_index",
            [alias, schema, table],
        ).fetchall()
        if not columns:
            raise PostgresError("таблица не найдена или в ней нет колонок", relation)
        raw_table = _raw_name(connection, schema, table)
        view_name = _view_name(connection, raw_table, slugify(table))
        names = normalize_column_names([column for column, _ in columns])
        converted = [
            _convert(original, name, data_type)
            for (original, data_type), name in zip(columns, names, strict=True)
        ]
        select = ", ".join(f"{expr} AS {_ident(name)}" for name, expr, *_ in converted)
        source = f"{_ident(alias)}.{_ident(schema)}.{_ident(table)}"
        connection.begin()
        try:
            connection.execute(
                f"CREATE OR REPLACE VIEW {_ident(raw_table)} AS SELECT * FROM {source}"
            )
            connection.execute(
                f"CREATE OR REPLACE VIEW {_ident(view_name)} AS "
                f"SELECT {select} FROM {_ident(raw_table)}"
            )
            # типы заданы БД, поэтому распознано всё непустое: parsed = non_empty
            counts = connection.execute(
                "SELECT "
                + ", ".join(_non_empty(name, physical) for name, _, physical, *_ in converted)
                + f" FROM {_ident(view_name)}"
            ).fetchone()
            plans = [
                _Plan(
                    TypedColumn(original, name, physical, semantic, None, count, count),
                    expr,
                    _rename_entries(original, name) + entries,
                )
                for (original, _), (name, expr, physical, semantic, entries), count in zip(
                    columns, converted, counts, strict=True
                )
            ]
            _write_registry(connection, raw_table, view_name, plans)
            _record_source(connection, DbSource(raw_table, view_name, SOURCE_KIND, relation))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except duckdb.Error as error:
        raise PostgresError(f"не удалось построить представление ({error})", relation) from None
    return TypedView(raw_table, view_name, tuple(plan.column for plan in plans))


def create_postgres_views(
    connection: duckdb.DuckDBPyConnection,
    tables: Sequence[tuple[str, str]],
    alias: str = ALIAS,
) -> tuple[tuple[TypedView, ...], tuple[PostgresError, ...]]:
    """Строит представления для таблиц ``(схема, имя)``; ошибка одной не мешает другим."""
    views: list[TypedView] = []
    errors: list[PostgresError] = []
    for schema, table in tables:
        try:
            views.append(create_postgres_view(connection, schema, table, alias))
        except PostgresError as error:
            errors.append(error)
    return tuple(views), tuple(errors)


def connect_postgres(
    connection: duckdb.DuckDBPyConnection,
    dsn: str | PgConnectionInfo,
    tables: Sequence[str] | None = None,
    alias: str = ALIAS,
) -> PostgresReport:
    """Подключает БД и строит представления выбранных таблиц (``None`` — всех).

    Дальше — общий конвейер, как для CSV: ``profile_tables`` и ``find_relations``.
    """
    info = attach_postgres(connection, dsn, alias)
    try:
        selected = select_tables(list_tables(connection, alias), tables)
    except Exception:
        detach_postgres(connection, alias)
        raise
    views, errors = create_postgres_views(connection, selected, alias)
    return PostgresReport(source=info.describe(), views=views, errors=errors)


def read_db_sources(connection: duckdb.DuckDBPyConnection) -> list[DbSource]:
    """Исходные таблицы подключённых БД для представлений (по имени «сырого» представления)."""
    _ensure_sources(connection)
    rows = connection.execute(
        f"SELECT raw_table, view_name, source_kind, relation FROM {SOURCES_TABLE} "
        "ORDER BY raw_table"
    ).fetchall()
    return [DbSource(*row) for row in rows]


def _parse_uri(text: str) -> dict[str, str]:
    parts = urlsplit(text)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    try:
        port = parts.port
    except ValueError:
        raise PostgresError("port должен быть целым числом от 1 до 65535") from None
    found = {
        "user": unquote(parts.username) if parts.username else None,
        "password": unquote(parts.password) if parts.password is not None else None,
        "host": parts.hostname,
        "port": str(port) if port is not None else None,
        "dbname": unquote(parts.path.lstrip("/")) or None,
    }
    return {**{k: v for k, v in found.items() if v is not None}, **params}


def _parse_keywords(text: str) -> dict[str, str]:
    params: dict[str, str] = {}
    position = 0
    while position < len(text):
        match = _KEYWORD.match(text, position)
        if match is None:
            raise PostgresError("не удалось разобрать строку подключения: ожидается key=value")
        key, quoted, plain = match.groups()
        params[key] = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else plain
        position = match.end()
    return params


def _number(value: str | None, default: int, name: str, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    if not value.isdigit() or not low <= int(value) <= high:
        raise PostgresError(f"{name} должен быть целым числом от {low} до {high}")
    return int(value)


def _quote(value: str) -> str:
    """Значение для строки libpq ``key='value'``."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_extension(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        connection.execute("LOAD postgres")
        return
    except duckdb.Error:
        pass
    try:
        connection.execute("INSTALL postgres")
        connection.execute("LOAD postgres")
    except duckdb.Error as error:
        raise PostgresError(
            "не удалось загрузить расширение DuckDB postgres: при первой установке "
            "нужен доступ к extensions.duckdb.org"
        ) from error


def _explain(message: str, info: PgConnectionInfo) -> str:
    """Причина ошибки подключения для пользователя; пароль вырезается из любого текста."""
    address = f"{info.host}:{info.port}"
    if "password authentication failed" in message or re.search(r'role ".*" does not', message):
        return "неверный пользователь или пароль"
    if re.search(r'database ".*" does not exist', message):
        return f"база данных «{info.dbname}» не найдена"
    if "no pg_hba.conf entry" in message:
        return f"сервер {address} не разрешает подключение пользователю «{info.user}»"
    if any(
        sign in message
        for sign in (
            "timeout expired", "Connection refused", "could not translate host name",
            "No route to host", "Network is unreachable", "Name or service not known",
        )
    ):  # fmt: skip
        return f"сервер {address} недоступен"
    detail = message.rpartition("failed: ")[2].strip() or message.strip()
    return f"не удалось подключиться к {address} ({detail.replace(info.password, '***')})"


def _convert(original: str, name: str, data_type: str) -> tuple[str, str, str, str, tuple]:
    """``(имя, выражение, физический тип, семантический тип, записи журнала)`` колонки."""
    column = _ident(original)
    id_like = bool(_ID_NAME.search(name))
    if data_type == "BOOLEAN":
        return name, column, data_type, "bool", ()
    if data_type in INTEGER_TYPES:
        return name, column, data_type, "id" if id_like else "integer", ()
    if data_type in FLOAT_TYPES or data_type.startswith("DECIMAL"):
        return name, column, data_type, "float", ()
    if data_type == "DATE":
        return name, column, data_type, "date", ()
    if data_type in ("TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"):
        return name, column, data_type, "datetime", ()
    if data_type == "TIMESTAMP WITH TIME ZONE":
        # момент времени переводится в UTC без зоны: так он одинаков при любой TimeZone сессии
        entry = ("cast_type", {"from": data_type, "to": "TIMESTAMP", "timezone": "UTC"})
        return name, f"timezone('UTC', {column})", "TIMESTAMP", "datetime", (entry,)
    if data_type == "VARCHAR":
        return name, column, data_type, "id" if id_like else "text", ()
    as_text = f"CAST({column} AS VARCHAR)"
    if data_type == "UUID" or data_type.startswith("ENUM"):
        entry = ("cast_type", {"from": data_type, "to": "VARCHAR"})
        return name, as_text, "VARCHAR", "id" if data_type == "UUID" else "text", (entry,)
    entry = ("keep_text", {"reason": "unsupported_type", "type": data_type})
    return name, as_text, "VARCHAR", "text", (entry,)


def _non_empty(name: str, physical_type: str) -> str:
    """Число непустых значений, как в профайлере: у текста пробелы — тоже пропуск."""
    column = _ident(name)
    if physical_type == "VARCHAR":
        return f"count(*) FILTER (WHERE trim({column}) <> '')"
    return f"count({column})"


def _raw_name(connection: duckdb.DuckDBPyConnection, schema: str, table: str) -> str:
    """Имя «сырого» представления: прежнее для той же таблицы, иначе ``pg_<схема>_<имя>``."""
    relation = f"{schema}.{table}"
    known = connection.execute(
        f"SELECT raw_table FROM {SOURCES_TABLE} WHERE source_kind = ? AND relation = ?",
        [SOURCE_KIND, relation],
    ).fetchone()
    if known:
        return known[0]
    here = "database_name = current_database()"
    taken = {
        r[0]
        for r in connection.execute(
            f"SELECT table_name FROM duckdb_tables() WHERE {here} "
            f"UNION SELECT view_name FROM duckdb_views() WHERE {here} AND NOT internal "
            f"UNION SELECT raw_table FROM {SOURCES_TABLE}"
        ).fetchall()
    }
    base = f"pg_{slugify(schema)}_{slugify(table)}"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}_{number}", number + 1
    return candidate


def _ensure_sources(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
            raw_table VARCHAR PRIMARY KEY, view_name VARCHAR, source_kind VARCHAR,
            relation VARCHAR, connected_at TIMESTAMP DEFAULT current_timestamp)"""
    )


def _record_source(connection: duckdb.DuckDBPyConnection, source: DbSource) -> None:
    connection.execute(f"DELETE FROM {SOURCES_TABLE} WHERE raw_table = ?", [source.raw_table])
    connection.execute(
        f"INSERT INTO {SOURCES_TABLE} (raw_table, view_name, source_kind, relation) "
        "VALUES (?, ?, ?, ?)",
        [source.raw_table, source.view_name, source.source_kind, source.relation],
    )
