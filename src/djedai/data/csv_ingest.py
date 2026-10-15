"""Загрузка сырых CSV в DuckDB (B1-1).

Каждый файл попадает в таблицу ``raw_<имя>``, все колонки — VARCHAR: значения
сохраняются как в файле (``00123`` остаётся ``00123``). Пустое поле без кавычек
записывается как NULL, пустая строка в кавычках ``""`` — как пустая строка.
Заголовок, разделитель и кавычки определяются по образцу; если определение
неоднозначно, наличие заголовка можно задать явно (``header=``).
Преобразование в числа и даты — отдельный этап (B1-2).
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import re
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
from charset_normalizer import from_bytes

# Временная техническая таблица со сведениями об исходных файлах; её судьба зависит
# от формата записи в DatasetCatalog (#59). Порядок полей совпадает с RawTable.
SOURCES_TABLE = "_djedai_sources"
_SOURCE_FIELDS = (
    "raw_table, original_name, row_count, columns, file_encoding, "
    "delimiter, quotechar, header, size_bytes, sha256"
)
SAMPLE_BYTES = 1 << 20  # Выделяем переменной 1 МБ памяти переменной
SAMPLE_ROWS = 50
DELIMITERS = (",", ";", "\t", "|")
QUOTES = ('"', "'")
FIELD_SIZE_LIMIT = 2**31 - 1  # у csv по умолчанию 131072 символа в поле
CHUNK_CHARS = 1 << 20
# Типичные кодировки для русскоязычных выгрузок: проверяются раньше остальных,
# иначе короткий кириллический текст нередко определяется как cp949/gb18030.
LIKELY_ENCODINGS = ["cp1251", "cp866", "koi8_r", "cp1252", "iso8859_5", "mac_cyrillic"]

_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh sch _ y _ e yu ya".split(),
        strict=True,
    )
)


class CsvReadError(Exception):
    """Файл не удалось прочитать; ``str(error)`` можно показывать пользователю."""

    def __init__(self, file_name: str, reason: str) -> None:
        super().__init__(f"Файл «{file_name}»: {reason}")
        self.file_name = file_name
        self.reason = reason


@dataclass(frozen=True)
class CsvDialect:  # результат анализа формата: разделитель, кавычки, заголовок, имена колонок
    delimiter: str
    quotechar: str
    header: bool
    columns: tuple[str, ...]


@dataclass(frozen=True)
class RawTable:  # метаданные загруженного файла: таблица, строки, кодировка, SHA-256 и т.д.
    raw_table: str
    original_name: str
    row_count: int
    columns: tuple[str, ...]
    encoding: str
    delimiter: str
    quotechar: str
    header: bool
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RawLoadReport:  # итог пакетной загрузки: успешные + ошибки
    loaded: tuple[RawTable, ...]
    errors: tuple[CsvReadError, ...]


def detect_encoding(sample: bytes, file_name: str = "") -> str:
    """Определяет кодировку по началу файла; возвращает имя кодека Python."""
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, final=False)  # обрыв на середине символа — не ошибка
        return "utf-8"
    except UnicodeDecodeError:
        pass
    match = from_bytes(sample, cp_isolation=LIKELY_ENCODINGS).best() or from_bytes(sample).best()
    if match is None:
        raise CsvReadError(file_name, "не удалось определить кодировку: сохраните файл в UTF-8")
    return "utf-8" if match.encoding == "ascii" else match.encoding


def detect_dialect(text: str, header: bool | None = None) -> CsvDialect:
    """Определяет разделитель, кавычки и наличие заголовка по первым строкам текста.

    ``header`` задаёт наличие заголовка явно; ``None`` — определить автоматически.
    """
    delimiter, quotechar = _detect_format(text)
    rows = _parse_rows(text, delimiter, quotechar)
    has_header = _has_header(rows) if header is None else header
    width = len(rows[0]) if rows else 1
    names = _column_names(rows[0] if has_header else [], width)
    return CsvDialect(delimiter=delimiter, quotechar=quotechar, header=has_header, columns=names)


@contextmanager
def _unbounded_fields() -> Iterator[None]:
    """Снимает лимит csv на размер поля, чтобы он не расходился с лимитами DuckDB."""
    previous = csv.field_size_limit(FIELD_SIZE_LIMIT)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


def _column_names(header_cells: list[str], width: int) -> tuple[str, ...]:
    """Имена колонок сырой таблицы: как в заголовке; пустые и повторы получают номер."""
    names: list[str] = []
    for index in range(width):
        base = header_cells[index].strip() if index < len(header_cells) else ""
        base = base or f"column{index + 1}"
        name, number = base, 2
        while name.lower() in {n.lower() for n in names}:
            name, number = f"{base}_{number}", number + 1
        names.append(name)
    return tuple(names)


def _parse_rows(text: str, delimiter: str, quotechar: str) -> list[list[str]]:
    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar=quotechar)
    try:
        with _unbounded_fields():
            for row in reader:
                if row:
                    rows.append(row)
                if len(rows) >= SAMPLE_ROWS:
                    break
    except csv.Error:
        pass
    return rows


def _detect_format(text: str) -> tuple[str, str]:
    """Выбирает пару (разделитель, кавычка), при которой строки самые ровные по числу полей.

    Приоритет признаков: доля строк с самым частым числом полей; кавычка действительно
    используется (какое-то поле начинается с неё); число полей. При равенстве побеждает
    более раннее сочетание: сначала ``"``, затем ``'``.
    """
    best, best_score = (DELIMITERS[0], QUOTES[0]), (0.0, False, 0)
    for quotechar in QUOTES:
        for delimiter in DELIMITERS:
            widths = [len(row) for row in _parse_rows(text, delimiter, quotechar)]
            if not widths:
                continue
            width, count = Counter(widths).most_common(1)[0]
            used = re.search(rf"(?:^|\n|{re.escape(delimiter)}){re.escape(quotechar)}", text)
            score = (count / len(widths) if width > 1 else 0.0, used is not None, width)
            if score > best_score:
                best, best_score = (delimiter, quotechar), score
    return best


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d[\d\s.,]*%?", value.strip()))


def _has_header(rows: list[list[str]]) -> bool:
    """Первая строка — заголовок, если она похожа на названия и не повторяется в данных.

    Заголовком не считается строка с числами, пустыми или повторяющимися ячейками, а
    также строка, значения которой встречаются в тех же колонках ниже. Если признаков
    «данных» нет, строка считается заголовком; для спорных файлов есть явный параметр.
    """
    if not rows:
        return True
    first = [cell.strip() for cell in rows[0]]
    if not all(first) or len(set(first)) != len(first):
        return False
    if any(_is_number(cell) for cell in first):
        return False
    return not any(
        index < len(row) and row[index].strip() == cell
        for row in rows[1:]
        for index, cell in enumerate(first)
    )


def raw_table_name(file_name: str, taken: set[str]) -> str:
    """Имя ``raw_<транслит>``: латиница, цифры и ``_``; повторы получают суффикс."""
    stem = Path(file_name).stem.lower()
    slug = "".join(_TRANSLIT.get(ch, ch) for ch in stem)
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_") or "table"
    name = f"raw_{slug}"
    candidate, number = name, 2
    while candidate in taken:
        candidate, number = f"{name}_{number}", number + 1
    return candidate


def load_raw_csv(
    connection: duckdb.DuckDBPyConnection,
    path: str | Path,
    original_name: str | None = None,
    *,
    header: bool | None = None,
) -> RawTable:
    """Загружает один CSV в ``raw_*``; при невозможности чтения бросает ``CsvReadError``.

    Загрузка атомарна: при любой ошибке таблица ``raw_*`` и запись об источнике не остаются.
    ``header`` задаёт наличие строки заголовка явно, ``None`` — определить автоматически.
    """
    path = Path(path)
    name = original_name or path.name
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            sample = handle.read(SAMPLE_BYTES)
    except OSError as error:
        raise CsvReadError(name, f"файл недоступен для чтения ({error.strerror})") from error
    if not sample.strip():
        raise CsvReadError(name, "файл пуст")

    encoding = detect_encoding(sample, name)
    try:
        text = codecs.getincrementaldecoder(encoding)(errors="ignore").decode(sample)
    except (LookupError, UnicodeError) as error:
        raise CsvReadError(name, f"кодировка {encoding} не поддерживается") from error
    dialect = detect_dialect(text, header)

    taken = {
        row[0] for row in connection.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    }
    table = raw_table_name(name, taken)
    with tempfile.TemporaryDirectory() as directory:
        utf8_copy = Path(directory) / "utf8.csv"
        digest = _transcode(path, utf8_copy, encoding, name)
        _create_table(connection, table, utf8_copy, dialect, name)
        try:
            expected = _count_records(utf8_copy, dialect, name) - (1 if dialect.header else 0)
            rows = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if rows != expected:
                raise CsvReadError(
                    name, f"прочитано {rows} строк из {expected}: файл разобран неполностью"
                )
            result = RawTable(
                raw_table=table,
                original_name=name,
                row_count=rows,
                columns=dialect.columns,
                encoding=encoding,
                delimiter=dialect.delimiter,
                quotechar=dialect.quotechar,
                header=dialect.header,
                size_bytes=size,
                sha256=digest,
            )
            _record_source(connection, result)
        except Exception:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            raise
    return result


def read_sources(connection: duckdb.DuckDBPyConnection) -> list[RawTable]:
    """Читает сохранённые сведения о загруженных файлах (в порядке загрузки)."""
    _ensure_sources_table(connection)
    cursor = connection.execute(
        f"SELECT {_SOURCE_FIELDS} FROM {SOURCES_TABLE} ORDER BY loaded_at, raw_table"
    )
    return [RawTable(*row[:3], tuple(row[3]), *row[4:]) for row in cursor.fetchall()]


def _ensure_sources_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
            raw_table VARCHAR PRIMARY KEY, original_name VARCHAR, row_count BIGINT,
            file_encoding VARCHAR, columns VARCHAR[], delimiter VARCHAR, quotechar VARCHAR,
            header BOOLEAN, size_bytes BIGINT, sha256 VARCHAR,
            loaded_at TIMESTAMP DEFAULT current_timestamp)"""
    )


def _record_source(connection: duckdb.DuckDBPyConnection, table: RawTable) -> None:
    _ensure_sources_table(connection)
    connection.execute(
        f"INSERT INTO {SOURCES_TABLE} ({_SOURCE_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            table.raw_table,
            table.original_name,
            table.row_count,
            list(table.columns),
            table.encoding,
            table.delimiter,
            table.quotechar,
            table.header,
            table.size_bytes,
            table.sha256,
        ],
    )


def load_raw_csvs(
    connection: duckdb.DuckDBPyConnection,
    paths: list[str | Path],
    *,
    header: bool | None = None,
) -> RawLoadReport:
    """Загружает несколько файлов; ошибка одного файла не мешает остальным.

    ``header`` (если задан) применяется ко всем файлам.
    """
    loaded: list[RawTable] = []
    errors: list[CsvReadError] = []
    for path in paths:
        try:
            loaded.append(load_raw_csv(connection, path, header=header))
        except CsvReadError as error:
            errors.append(error)
    return RawLoadReport(loaded=tuple(loaded), errors=tuple(errors))


def _transcode(source: Path, target: Path, encoding: str, name: str) -> str:
    """Переписывает файл в UTF-8 потоково; возвращает SHA-256 исходных байтов."""
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        with source.open("rb") as reader, target.open("w", encoding="utf-8", newline="") as writer:
            while chunk := reader.read(CHUNK_CHARS):
                digest.update(chunk)
                writer.write(decoder.decode(chunk))
            writer.write(decoder.decode(b"", final=True))
    except UnicodeDecodeError as error:
        raise CsvReadError(
            name, f"файл не соответствует кодировке {encoding} (байт {error.start})"
        ) from error
    return digest.hexdigest()


def _count_records(source: Path, dialect: CsvDialect, name: str) -> int:
    """Независимый подсчёт записей (включая заголовок) для сверки с загруженным."""
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=dialect.delimiter, quotechar=dialect.quotechar)
        try:
            with _unbounded_fields():
                return sum(1 for row in reader if row)
        except csv.Error as error:
            raise CsvReadError(name, f"нарушена разметка кавычек ({error})") from error


def _create_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    source: Path,
    dialect: CsvDialect,
    name: str,
) -> None:
    columns = ", ".join("'{}': 'VARCHAR'".format(c.replace("'", "''")) for c in dialect.columns)
    try:
        connection.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_csv(?, delim=?, quote=?, escape=?, '
            "header=?, all_varchar=true, allow_quoted_nulls=false, auto_detect=false, "
            f"columns={{{columns}}})",
            [str(source), dialect.delimiter, dialect.quotechar, dialect.quotechar, dialect.header],
        )
    except duckdb.Error as error:
        raise CsvReadError(name, _explain(error, dialect)) from error


def _explain(error: duckdb.Error, dialect: CsvDialect) -> str:
    message = str(error)
    line = re.search(r"Line:? (\d+)", message)
    where = f" (строка {line.group(1)})" if line else ""
    return (
        f"не удалось разобрать содержимое{where}: число полей не совпадает с заголовком "
        f"или нарушены кавычки; определённый разделитель {dialect.delimiter!r}"
    )
