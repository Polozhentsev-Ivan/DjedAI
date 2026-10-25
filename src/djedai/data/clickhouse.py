"""ClickHouse как источник только для чтения (B1-9).

Выбранные таблицы копируются в файл DuckDB датасета (решение #61): ClickHouse отдаёт
``SELECT * … FORMAT Parquet`` через HTTP-интерфейс, файл читается ``read_parquet`` в
«сырую» таблицу ``ch_<база>_<таблица>``, а над ней строится типизированное
представление ``<таблица>``, как у PostgreSQL. Дальше работают общий профайлер, поиск
связей и все инструменты аналитики; SQL агента к ClickHouse не уходит.

Объём ограничен дважды: до загрузки суммарные строки и байты выбранных таблиц сверяются
с ``ClickHouseLimits``, а каждая выгрузка получает от сервера не больше остатка общего
порога и пересчитывается после загрузки — рост источника между подсчётом и выгрузкой не
обходит порог. Профиль, при котором сервер может молча усечь ответ (``*_overflow_mode`` не
``throw``, ``limit``/``offset``), отклоняется, а число загруженных строк сверяется с
источником: усечённая копия не публикуется под видом полной.

Запись запрещена дважды: каждый запрос идёт с ``readonly=2`` (или под пользователем с
``readonly=1``, которому настройки менять нельзя), и после подключения проверяется, что
сервер действительно включил режим только для чтения. Гарантию даёт read-only учётная
запись на стороне БД (``infra/clickhouse/roles.sql``).

Пароль передаётся только в заголовке ``Authorization``: в URL запроса, ``repr`` и
сообщениях об ошибках его нет. В файле DuckDB подключение не сохраняется.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from djedai.data.csv_ingest import slugify
from djedai.data.postgres import (
    SOURCES_TABLE,
    DbSource,
    _convert,
    _ensure_sources,
    _record_source,
)
from djedai.data.typed_views import (
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

SOURCE_KIND = "clickhouse"
DEFAULT_DATABASE = "default"
CONNECT_TIMEOUT = 10
MAX_CONNECT_TIMEOUT = 300
QUERY_TIMEOUT = 300
DSN_KEYS = ("secure", "connect_timeout")
SCHEMES = ("http", "https", "clickhouse")
# Parquet без бинарных строк: String/FixedString/Enum читаются в DuckDB как VARCHAR
PARQUET_SETTINGS = {
    "output_format_parquet_string_as_string": "1",
    "output_format_parquet_fixed_string_as_fixed_byte_array": "0",
    "output_format_parquet_enum_as_byte_array": "1",
}
# Parquet отдаёт эти типы байтами или числом; на сервере они переводятся в текст
_AS_TEXT = re.compile(r"U?Int(?:128|256)|IPv[46]")
_WRAPPERS = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")
# серверные лимиты *_overflow_mode=throw: TOO_MANY_ROWS, TOO_MANY_BYTES, TOO_MANY_ROWS_OR_BYTES
_TOO_LARGE = re.compile(r"\(TOO_MANY_(?:ROWS|BYTES)\w*\)")
# лимиты, при превышении которых режим break отдаёт часть строк как обычный ответ
OVERFLOW_LIMITS = (
    ("result_overflow_mode", ("max_result_rows", "max_result_bytes")),
    ("read_overflow_mode", ("max_rows_to_read", "max_bytes_to_read")),
    ("read_overflow_mode_leaf", ("max_rows_to_read_leaf", "max_bytes_to_read_leaf")),
    ("timeout_overflow_mode", ("max_execution_time",)),
)
TRUNCATING_SETTINGS = ("limit", "offset")  # обрезают любой SELECT


class ClickHouseError(Exception):
    """Подключиться или прочитать таблицу не удалось; ``str(error)`` можно показывать.

    Пароль в сообщение не попадает. ``table`` — ``база.таблица`` при ошибке одной таблицы.
    """

    def __init__(self, reason: str, table: str | None = None) -> None:
        super().__init__(f"Таблица «{table}»: {reason}" if table else f"ClickHouse: {reason}")
        self.reason = reason
        self.table = table


class SourceTooLargeError(ClickHouseError):
    """Выбранные таблицы больше порога материализации; текст безопасен для пользователя."""


@dataclass(frozen=True)
class ClickHouseLimits:
    """Порог материализации: суммарно по выбранным таблицам и на сервере для каждой выгрузки."""

    max_rows: int = 5_000_000
    max_bytes: int = 1_000_000_000
    query_timeout: int = QUERY_TIMEOUT


@dataclass(frozen=True)
class ClickHouseHttp:  # HTTP-интерфейс ClickHouse; пароль не попадает в repr и URL запроса
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = DEFAULT_DATABASE
    secure: bool = False
    connect_timeout: int = CONNECT_TIMEOUT

    @classmethod
    def from_url(cls, url: str, *, default_database: str = DEFAULT_DATABASE) -> ClickHouseHttp:
        """Разбирает ``http(s)://user:password@host:port/database``; спецсимволы — %XX.

        ``clickhouse://`` — тот же HTTP-интерфейс (``?secure=1`` — HTTPS). Порт по
        умолчанию 8123 (8443 для HTTPS). Пользователь и пароль обязательны.
        """
        text = url.strip()
        if not text:
            raise ClickHouseError("строка подключения пуста")
        parts = urllib.parse.urlsplit(text)
        if parts.scheme not in SCHEMES:
            raise ClickHouseError(
                "адрес ClickHouse должен начинаться с http://, https:// или clickhouse://"
            )
        params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        unknown = sorted(set(params) - set(DSN_KEYS))
        if unknown:
            raise ClickHouseError(f"параметры не поддерживаются: {', '.join(unknown)}")
        if not parts.username:
            raise ClickHouseError("не указан пользователь")
        if not parts.password:
            raise ClickHouseError("не указан пароль")
        secure_text = params.get("secure", "")
        if secure_text not in ("", "0", "1", "true", "false"):
            raise ClickHouseError("secure должен быть 0, 1, true или false")
        secure = parts.scheme == "https" or secure_text in ("1", "true")
        try:
            port = parts.port
        except ValueError:
            raise ClickHouseError("port должен быть целым числом от 1 до 65535") from None
        database = urllib.parse.unquote(parts.path.strip("/"))
        if "/" in database:
            raise ClickHouseError("в адресе можно указать только одну базу данных")
        return cls(
            host=parts.hostname or "localhost",
            port=port or (8443 if secure else 8123),
            user=urllib.parse.unquote(parts.username),
            password=urllib.parse.unquote(parts.password),
            database=database or default_database,
            secure=secure,
            connect_timeout=_number(
                params.get("connect_timeout"),
                CONNECT_TIMEOUT,
                "connect_timeout",
                1,
                MAX_CONNECT_TIMEOUT,
            ),
        )

    def describe(self) -> str:
        """Адрес без пароля: ``user@host:port/database``."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"

    def query(
        self,
        sql: str,
        data: bytes | None = None,
        *,
        settings: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Выполняет ``sql``; при ``data`` запрос идёт в URL, а тело — входные данные."""
        with self._open(sql, data, settings, timeout) as response:
            return response.read().decode()

    def download(
        self,
        sql: str,
        target: Path,
        *,
        settings: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> int:
        """Пишет ответ на ``sql`` в файл потоком; больше ``max_bytes`` — ошибка, файл удаляется."""
        written = 0
        try:
            with self._open(sql, None, settings, timeout) as response, target.open("wb") as file:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise SourceTooLargeError(
                            f"выгрузка больше {max_bytes} байт — уменьшите выбор таблиц"
                        )
                    file.write(chunk)
        except (ClickHouseError, OSError) as error:
            target.unlink(missing_ok=True)
            if isinstance(error, ClickHouseError):
                raise
            raise ClickHouseError(f"не удалось сохранить выгрузку ({error.strerror})") from None
        return written

    def _open(
        self,
        sql: str,
        data: bytes | None,
        settings: Mapping[str, str] | None,
        timeout: float | None,
    ):
        params = {"database": self.database, **(settings or {})}
        if data is not None:
            params["query"] = sql
        scheme = "https" if self.secure else "http"
        url = f"{scheme}://{self.host}:{self.port}/?{urllib.parse.urlencode(params)}"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request = urllib.request.Request(
            url,
            data=sql.encode() if data is None else data,
            headers={"Authorization": f"Basic {token}"},
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.connect_timeout)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            if _TOO_LARGE.search(detail):
                raise SourceTooLargeError(
                    "источник больше порога загрузки — уменьшите выбор таблиц"
                ) from None
            raise ClickHouseError(f"{self.describe()}: {self._hide(detail)}") from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            message = f"не удалось подключиться к {self.describe()}: {self._hide(str(reason))}"
            raise ClickHouseError(message) from None

    def _hide(self, text: str) -> str:
        return text.replace(self.password, "***")


@dataclass(frozen=True)
class ClickHouseTable:  # таблица источника: имя, объём и колонки ``(имя, тип ClickHouse)``
    name: str
    rows: int
    bytes: int
    columns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClickHouseSession:  # проверенное подключение и настройки каждого запроса
    client: ClickHouseHttp
    settings: Mapping[str, str]
    limits: ClickHouseLimits

    def query(self, sql: str) -> str:
        return self.client.query(sql, settings=self.settings, timeout=self.limits.query_timeout)


@dataclass(frozen=True)
class MaterializedTable:  # загруженная таблица и её фактический объём
    view: TypedView
    rows: int
    bytes: int


@dataclass(frozen=True)
class ClickHouseReport:  # итог подключения: источник, построенные представления
    source: str
    views: tuple[TypedView, ...]


def parse_dsn(dsn: str) -> ClickHouseHttp:
    """Строка подключения источника; без базы в адресе — ``default``."""
    return ClickHouseHttp.from_url(dsn)


def open_session(
    dsn: str | ClickHouseHttp, limits: ClickHouseLimits | None = None
) -> ClickHouseSession:
    """Подключается и проверяет, что запросы выполняются только на чтение.

    Пользователю с ``readonly=1`` сервер не даёт менять настройки: тогда серверных
    лимитов нет, объём ограничивают подсчёт до и после выгрузки и её размер, а профиль
    сервера должен исключать усечение ответа (``check_complete_results``).
    """
    client = parse_dsn(dsn) if isinstance(dsn, str) else dsn
    limits = limits or ClickHouseLimits()
    current = client.query("SELECT getSetting('readonly')").strip()
    settings: dict[str, str] = {}
    if current != "1":
        settings = {
            "readonly": "2",
            "max_execution_time": str(limits.query_timeout),
            "max_result_rows": str(limits.max_rows),
            "max_result_bytes": str(limits.max_bytes),
            "result_overflow_mode": "throw",
            "max_rows_to_read": str(limits.max_rows),
            "max_bytes_to_read": str(limits.max_bytes),
            "read_overflow_mode": "throw",
            "read_overflow_mode_leaf": "throw",
            "timeout_overflow_mode": "throw",
            "limit": "0",
            "offset": "0",
        }
    session = ClickHouseSession(client, settings, limits)
    if session.query("SELECT getSetting('readonly')").strip() not in ("1", "2"):
        raise ClickHouseError("сервер не перевёл запросы в режим только для чтения")
    check_complete_results(session)
    return session


def check_complete_results(session: ClickHouseSession) -> None:
    """Отклоняет профиль, при котором сервер может вернуть часть строк без ошибки.

    Проверяются действующие настройки запроса (профиль пользователя и настройки сессии):
    ``limit``/``offset`` должны быть 0, а у каждого заданного лимита — режим ``throw``.
    """
    names = [*TRUNCATING_SETTINGS]
    for mode, limits in OVERFLOW_LIMITS:
        names += [mode, *limits]
    listed = ", ".join(_ch_literal(name) for name in names)
    values = {
        name: str(value)
        for name, value in _rows(
            session.query(
                f"SELECT name, value FROM system.settings WHERE name IN ({listed}) "
                "FORMAT JSONCompactEachRow"
            )
        )
    }
    problems = [
        f"{name}={values[name]}" for name in TRUNCATING_SETTINGS if values.get(name, "0") != "0"
    ]
    for mode, limits in OVERFLOW_LIMITS:
        limited = [name for name in limits if values.get(name, "0") not in ("0", "0.")]
        if limited and values.get(mode, "throw") != "throw":
            problems.append(f"{mode}={values[mode]} при {limited[0]}={values[limited[0]]}")
    if problems:
        raise ClickHouseError(
            "профиль пользователя допускает усечённые ответы ("
            + ", ".join(problems)
            + "); задайте *_overflow_mode=throw и limit/offset=0 или подключите пользователя "
            "с readonly=2"
        )


def list_tables(session: ClickHouseSession) -> list[ClickHouseTable]:
    """Таблицы и представления базы подключения с объёмом; без временных таблиц."""
    tables = _rows(
        session.query(
            "SELECT name, total_rows, total_bytes FROM system.tables "
            "WHERE database = currentDatabase() AND NOT is_temporary ORDER BY name "
            "FORMAT JSONCompactEachRow"
        )
    )
    columns: dict[str, list[tuple[str, str]]] = {}
    for table, name, data_type in _rows(
        session.query(
            "SELECT table, name, type FROM system.columns WHERE database = currentDatabase() "
            "ORDER BY table, position FORMAT JSONCompactEachRow"
        )
    ):
        columns.setdefault(table, []).append((name, data_type))
    return [
        ClickHouseTable(name, _count(rows), _count(size), tuple(columns.get(name, ())))
        for name, rows, size in tables
    ]


def select_tables(
    available: Sequence[ClickHouseTable], requested: Sequence[str] | None, database: str
) -> list[ClickHouseTable]:
    """Выбранные таблицы: имя или ``база.имя`` базы подключения; ``None`` — все.

    Неизвестные имена дают ``ClickHouseError`` со списком; повторы убираются.
    """
    if requested is None:
        if not available:
            raise ClickHouseError("в базе нет доступных таблиц")
        return list(available)
    known = {table.name: table for table in available}
    selected: list[ClickHouseTable] = []
    missing: list[str] = []
    for name in requested:
        prefix, _, table = name.rpartition(".")
        if (prefix and prefix != database) or table not in known:
            missing.append(name)
        elif known[table] not in selected:
            selected.append(known[table])
    if missing:
        raise ClickHouseError(f"таблицы не найдены: {', '.join(missing)}")
    if not selected:
        raise ClickHouseError("не выбрано ни одной таблицы")
    return selected


def measure(session: ClickHouseSession, tables: Sequence[ClickHouseTable]) -> list[ClickHouseTable]:
    """Число строк представлений и таблиц без статистики считается запросом ``count()``."""
    measured = []
    for table in tables:
        if table.rows < 0:
            rows = _count_rows(session, table)
            table = ClickHouseTable(table.name, rows, max(table.bytes, 0), table.columns)
        measured.append(table)
    return measured


def check_volume(tables: Sequence[ClickHouseTable], limits: ClickHouseLimits) -> None:
    """Отклоняет материализацию, если выбранные таблицы вместе больше порога."""
    rows = sum(table.rows for table in tables)
    size = sum(max(table.bytes, 0) for table in tables)
    if rows > limits.max_rows:
        raise SourceTooLargeError(
            f"источник слишком велик для загрузки: {rows} строк > {limits.max_rows}; "
            "выберите меньше таблиц"
        )
    if size > limits.max_bytes:
        raise SourceTooLargeError(
            f"источник слишком велик для загрузки: {size} байт > {limits.max_bytes}; "
            "выберите меньше таблиц"
        )


def materialize_table(
    connection: duckdb.DuckDBPyConnection,
    session: ClickHouseSession,
    table: ClickHouseTable,
    folder: Path,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> MaterializedTable:
    """Копирует таблицу в DuckDB и строит над ней представления; запись реестра атомарна.

    ``folder`` — каталог датасета для временного Parquet; файл удаляется в любом случае.
    ``max_rows``/``max_bytes`` — остаток общего порога (по умолчанию весь порог): сервер
    отдаёт не больше, загруженные строки пересчитываются. Копия, в которой строк меньше,
    чем в источнике до и после выгрузки, отклоняется как неполная.
    """
    limits = session.limits
    max_rows = limits.max_rows if max_rows is None else max_rows
    max_bytes = limits.max_bytes if max_bytes is None else max_bytes
    database = session.client.database
    relation = f"{database}.{table.name}"
    if not table.columns:
        raise ClickHouseError("таблица не найдена или в ней нет колонок", relation)
    select = ", ".join(_export(name, data_type) for name, data_type in table.columns)
    source = f"{_ch_ident(database)}.{_ch_ident(table.name)}"
    parquet = folder / f".clickhouse-{slugify(table.name)}.parquet"
    settings = {**session.settings, **PARQUET_SETTINGS}
    if session.settings:
        # Результат — не больше остатка общего порога; лимиты чтения остаются общими, ведь
        # представление может прочитать больше строк, чем вернёт. 0 у ClickHouse — «без
        # лимита», поэтому исчерпанный остаток проверяется после загрузки.
        settings |= {
            "max_result_rows": str(max(max_rows, 1)),
            "max_result_bytes": str(max(max_bytes, 1)),
        }
    try:
        size = session.client.download(
            f"SELECT {select} FROM {source} FORMAT Parquet",
            parquet,
            settings=settings,
            max_bytes=max(max_bytes, 1),
            timeout=limits.query_timeout,
        )
        _ensure_tables(connection)
        _ensure_sources(connection)
        raw_table = _raw_name(connection, database, table.name)
        view_name = _view_name(connection, raw_table, slugify(table.name))
        connection.begin()
        try:
            connection.execute(
                f"CREATE OR REPLACE TABLE {_ident(raw_table)} AS "
                f"SELECT * FROM read_parquet({_lit(str(parquet))})"
            )
            rows = connection.execute(f"SELECT count(*) FROM {_ident(raw_table)}").fetchone()[0]
            if rows > max_rows:
                raise SourceTooLargeError(
                    f"источник больше порога загрузки ({limits.max_rows} строк на все "
                    "таблицы) — уменьшите выбор таблиц"
                )
            if rows < table.rows:
                # источник мог уменьшиться во время выгрузки: решает повторный подсчёт
                now = _count_rows(session, table)
                if rows < now:
                    raise ClickHouseError(
                        f"выгрузка неполная: получено {rows} строк из {now} — сервер мог "
                        "усечь ответ; проверьте профиль пользователя и повторите подключение"
                    )
            columns = connection.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE database_name = current_database() AND table_name = ? "
                "ORDER BY column_index",
                [raw_table],
            ).fetchall()
            names = normalize_column_names([column for column, _ in columns])
            converted = [
                _convert(original, name, data_type)
                for (original, data_type), name in zip(columns, names, strict=True)
            ]
            select_view = ", ".join(f"{expr} AS {_ident(name)}" for name, expr, *_ in converted)
            connection.execute(
                f"CREATE OR REPLACE VIEW {_ident(view_name)} AS "
                f"SELECT {select_view} FROM {_ident(raw_table)}"
            )
            plans = [
                _Plan(
                    TypedColumn(original, name, physical, semantic, None, None, None),
                    expr,
                    _rename_entries(original, name) + entries,
                )
                for (original, _), (name, expr, physical, semantic, entries) in zip(
                    columns, converted, strict=True
                )
            ]
            _write_registry(connection, raw_table, view_name, plans)
            _record_source(connection, DbSource(raw_table, view_name, SOURCE_KIND, relation))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except ClickHouseError as error:
        if error.table:
            raise
        raise type(error)(error.reason, relation) from None
    except duckdb.Error as error:
        raise ClickHouseError(f"не удалось загрузить таблицу ({error})", relation) from None
    finally:
        parquet.unlink(missing_ok=True)
    return MaterializedTable(
        TypedView(raw_table, view_name, tuple(plan.column for plan in plans)), rows, size
    )


def connect_clickhouse(
    connection: duckdb.DuckDBPyConnection,
    dsn: str | ClickHouseHttp,
    tables: Sequence[str] | None,
    folder: Path,
    limits: ClickHouseLimits | None = None,
) -> ClickHouseReport:
    """Подключает БД, проверяет объём и копирует выбранные таблицы (``None`` — все).

    Любая ошибка таблицы прерывает загрузку: частичный датасет не публикуется.
    Дальше — общий конвейер: ``profile_table`` и ``find_relations``.
    """
    session = open_session(dsn, limits)
    selected = select_tables(list_tables(session), tables, session.client.database)
    selected = measure(session, selected)
    check_volume(selected, session.limits)
    rows, size = session.limits.max_rows, session.limits.max_bytes
    views = []
    for table in selected:
        loaded = materialize_table(
            connection, session, table, folder, max_rows=rows, max_bytes=size
        )
        rows, size = rows - loaded.rows, size - loaded.bytes
        views.append(loaded.view)
    return ClickHouseReport(source=session.client.describe(), views=tuple(views))


def _export(name: str, data_type: str) -> str:
    """Колонка выгрузки; типы, которые Parquet отдаёт байтами, переводятся в текст."""
    inner = data_type
    while match := _WRAPPERS.match(inner):
        inner = match[1]
    column = _ch_ident(name)
    if _AS_TEXT.fullmatch(inner):
        return f"toString({column}) AS {column}"
    return column


def _raw_name(connection: duckdb.DuckDBPyConnection, database: str, table: str) -> str:
    """Имя «сырой» таблицы: прежнее для той же таблицы, иначе ``ch_<база>_<имя>``."""
    relation = f"{database}.{table}"
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
    base = f"ch_{slugify(database)}_{slugify(table)}"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}_{number}", number + 1
    return candidate


def _count_rows(session: ClickHouseSession, table: ClickHouseTable) -> int:
    source = f"{_ch_ident(session.client.database)}.{_ch_ident(table.name)}"
    try:
        return int(session.query(f"SELECT count() FROM {source}").strip())
    except SourceTooLargeError:
        raise
    except ClickHouseError as error:
        raise ClickHouseError(error.reason, table.name) from None
    except ValueError:
        raise ClickHouseError("не удалось посчитать строки", table.name) from None


def _ch_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ch_ident(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _rows(text: str) -> list[list]:
    return [json.loads(line) for line in text.splitlines() if line]


def _count(value: str | int | None) -> int:
    """Счётчик ``system.tables`` (UInt64 приходит строкой); ``NULL`` — нет статистики, ``-1``."""
    return -1 if value is None else int(value)


def _number(value: str | None, default: int, name: str, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    if not value.isdigit() or not low <= int(value) <= high:
        raise ClickHouseError(f"{name} должен быть целым числом от {low} до {high}")
    return int(value)
