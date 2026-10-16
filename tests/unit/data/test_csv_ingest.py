from pathlib import Path

import duckdb
import pytest

from djedai.data.csv_ingest import (
    CsvReadError,
    _column_names,
    detect_dialect,
    detect_encoding,
    load_raw_csv,
    load_raw_csvs,
    raw_table_name,
    read_sources,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "csv"

ORDER_COLUMNS = ("order_id", "customer", "amount", "comment")
ORDERS_COMMA = [
    ("00123", "Иванов Пётр", "1 234.56", "Доставка завтра"),
    ("00124", "ООО «Ромашка»", "99.90", 'Позвонить; "срочно"'),
    ("00125", "Сидорова Анна", "0.00", None),
]
ORDERS_SEMICOLON = [(a, b, c.replace(".", ","), d) for a, b, c, d in ORDERS_COMMA]
CUSTOMERS = [("007", "Иванов Пётр", "Москва"), ("008", "Сидорова Анна", "Санкт-Петербург")]


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def rows(con, table):
    return con.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


@pytest.mark.parametrize(
    ("file", "encoding", "delimiter", "expected"),
    [
        ("orders_utf8_comma.csv", "utf-8", ",", ORDERS_COMMA),
        ("orders_cp1251_semicolon.csv", "cp1251", ";", ORDERS_SEMICOLON),
    ],
)
def test_orders_match_file_values(con, file, encoding, delimiter, expected):
    table = load_raw_csv(con, FIXTURES / file)

    assert table.raw_table == "raw_orders_" + (
        "utf8_comma" if encoding == "utf-8" else "cp1251_semicolon"
    )
    assert table.encoding == encoding
    assert table.delimiter == delimiter
    assert table.header is True
    assert table.columns == ORDER_COLUMNS
    assert table.row_count == 3
    assert rows(con, table.raw_table) == expected


@pytest.mark.parametrize(
    ("file", "encoding", "delimiter"),
    [
        ("customers_utf8_semicolon.csv", "utf-8", ";"),
        ("customers_cp1251_comma.csv", "cp1251", ","),
    ],
)
def test_customers_keep_leading_zeros_as_strings(con, file, encoding, delimiter):
    table = load_raw_csv(con, FIXTURES / file)

    assert (table.encoding, table.delimiter) == (encoding, delimiter)
    assert rows(con, table.raw_table) == CUSTOMERS
    types = {row[0]: row[1] for row in con.execute(f'DESCRIBE "{table.raw_table}"').fetchall()}
    assert set(types.values()) == {"VARCHAR"}


def test_source_file_details_are_recorded(con):
    path = FIXTURES / "orders_cp1251_semicolon.csv"

    table = load_raw_csv(con, path, original_name="Заказы.csv")

    assert table.original_name == "Заказы.csv"
    assert table.raw_table == "raw_zakazy"
    assert table.size_bytes == path.stat().st_size == 156
    assert len(table.sha256) == 64


def test_several_files_are_loaded_into_separate_tables(con):
    report = load_raw_csvs(
        con,
        [
            FIXTURES / "orders_utf8_comma.csv",
            FIXTURES / "customers_cp1251_comma.csv",
            FIXTURES / "customers_utf8_semicolon.csv",
        ],
    )

    assert report.errors == ()
    names = [t.raw_table for t in report.loaded]
    assert len(set(names)) == 3
    assert [t.row_count for t in report.loaded] == [3, 2, 2]


def test_same_file_twice_gets_distinct_table_names(con):
    path = FIXTURES / "customers_utf8_semicolon.csv"

    report = load_raw_csvs(con, [path, path])

    assert [t.raw_table for t in report.loaded] == [
        "raw_customers_utf8_semicolon",
        "raw_customers_utf8_semicolon_2",
    ]


@pytest.mark.parametrize(
    ("file", "reason"),
    [("empty.csv", "файл пуст"), ("missing.csv", "файл недоступен")],
)
def test_unreadable_file_gives_explanation(con, file, reason):
    with pytest.raises(CsvReadError) as error:
        load_raw_csv(con, FIXTURES / file)

    assert reason in str(error.value)
    assert str(error.value).startswith(f"Файл «{file}»")


def test_binary_file_is_rejected_with_explanation(con):
    with pytest.raises(CsvReadError) as error:
        load_raw_csv(con, FIXTURES / "binary.csv")

    assert "binary.csv" in str(error.value)
    assert con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0] == 0


def test_one_bad_file_does_not_block_the_others(con):
    report = load_raw_csvs(con, [FIXTURES / "empty.csv", FIXTURES / "orders_utf8_comma.csv"])

    assert [t.row_count for t in report.loaded] == [3]
    assert [e.file_name for e in report.errors] == ["empty.csv"]


def test_ragged_rows_report_a_reason(con, tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text("a,b\n1,2\n3,4,5,6\n", encoding="utf-8")

    with pytest.raises(CsvReadError) as error:
        load_raw_csv(con, path)

    assert "ragged.csv" in str(error.value)
    assert "не удалось разобрать" in str(error.value)


def test_bytes_invalid_beyond_the_sample_are_reported(con, tmp_path, monkeypatch):
    monkeypatch.setattr("djedai.data.csv_ingest.SAMPLE_BYTES", 16)
    path = tmp_path / "late_cp1251.csv"
    path.write_bytes(b"a,b\n1,2\n3,4\n5,6\n" + "7,привет\n".encode("cp1251"))

    with pytest.raises(CsvReadError) as error:
        load_raw_csv(con, path)

    assert "late_cp1251.csv" in str(error.value)
    assert "не соответствует кодировке utf-8" in str(error.value)


def test_multiline_quoted_field_and_blank_lines(con, tmp_path):
    path = tmp_path / "multiline.csv"
    path.write_text('id,note\n001,"строка 1\nстрока 2"\n\n002,x\n', encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert rows(con, table.raw_table) == [("001", "строка 1\nстрока 2"), ("002", "x")]


def test_duplicate_and_empty_header_names_are_made_unique():
    assert _column_names(["id", "ID", "x", "id"], 4) == ("id", "ID_2", "x", "id_3")
    assert _column_names(["a", "", " "], 3) == ("a", "column2", "column3")


def test_single_column_file(con, tmp_path):
    path = tmp_path / "single.csv"
    path.write_text("city\nМосква\nКазань\n", encoding="utf-8")

    table = load_raw_csv(con, path)

    assert rows(con, table.raw_table) == [("Казань",), ("Москва",)]


def test_utf8_bom_and_utf16_are_supported(con, tmp_path):
    bom = tmp_path / "bom.csv"
    bom.write_bytes("id;имя\r\n001;Анна\r\n".encode("utf-8-sig"))
    utf16 = tmp_path / "utf16.csv"
    utf16.write_bytes("id;имя\r\n002;Борис\r\n".encode("utf-16"))

    first = load_raw_csv(con, bom)
    second = load_raw_csv(con, utf16)

    assert (first.encoding, first.columns) == ("utf-8-sig", ("id", "имя"))
    assert rows(con, first.raw_table) == [("001", "Анна")]
    assert (second.encoding, second.columns) == ("utf-16", ("id", "имя"))
    assert rows(con, second.raw_table) == [("002", "Борис")]


def test_header_only_file_creates_empty_table(con, tmp_path):
    path = tmp_path / "only_header.csv"
    path.write_text("a;b\n", encoding="utf-8")

    table = load_raw_csv(con, path)

    assert (table.columns, table.row_count) == (("a", "b"), 0)


def test_headerless_file_gets_default_column_names(con, tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text("1,2\n3,4\n", encoding="utf-8")

    table = load_raw_csv(con, path)

    assert table.header is False
    assert rows(con, table.raw_table) == [("1", "2"), ("3", "4")]


@pytest.mark.parametrize(
    ("text", "delimiter"),
    [
        ("a,b,c\n1,2,3\n", ","),
        ("a;b;c\n1,5;2;3\n", ";"),
        ("a\tb\tc\n1\t2\t3\n", "\t"),
        ("a|b|c\n1|2|3\n", "|"),
        ('"a;x",b\n"1;2",3\n', ","),
        ("single\nvalue\n", ","),
    ],
)
def test_delimiter_detection(text, delimiter):
    assert detect_dialect(text).delimiter == delimiter


@pytest.mark.parametrize(
    ("text", "header"),
    [
        ("id,name\n1,Ann\n", True),
        ("1,Ann\n2,Bob\n", False),
        ("id,id\n1,2\n", True),
        ("a,,c\n1,2,3\n", True),
        (",A,B\n1,x,2\n", True),
        (",name\n1,Alice\n2,Bob\n", True),
        ("id,name,Name,,name\n1,a,b,c,d\n", True),
        ("id,created,created\n1,2026-03-25,2026-03-26\n", True),
        ("only\n", True),
        # повторы и пропуски без числовых колонок ниже: доказательств заголовка нет
        ("a,a\nb,c\n", False),
        ("Иван,Иван,,Москва\nАнна,Ольга,,Казань\n", False),
        ("a,,c\n", False),
        (",,\n1,2,3\n", False),
        ("2026-03-25,2026-03-25\n2026-03-26,2026-03-27\n", False),
        ("25.03.2026,25.03.2026\n26.03.2026,27.03.2026\n", False),
        # значения первой строки повторяются ниже: это данные
        ("id,id\nid,2\n", False),
    ],
)
def test_header_detection(text, header):
    assert detect_dialect(text).header is header


def test_unnamed_index_column_header_is_detected(con, tmp_path):
    path = tmp_path / "index.csv"
    path.write_text(",name\n1,Alice\n2,Bob\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert table.header is True
    assert table.columns == ("column1", "name")
    assert rows(con, table.raw_table) == [("1", "Alice"), ("2", "Bob")]


def test_dated_records_without_header_keep_both_rows(con, tmp_path):
    path = tmp_path / "dates.csv"
    path.write_text(
        "2026-03-25,2026-03-25\n2026-03-26,2026-03-27\n", encoding="utf-8", newline="\n"
    )

    table = load_raw_csv(con, path)

    assert table.header is False
    assert table.row_count == 2


def test_header_with_duplicate_and_empty_names_gives_unique_columns(con, tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("id,name,Name,,name\n1,a,b,c,d\n2,e,f,g,h\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert table.header is True
    assert table.columns == ("id", "name", "Name_2", "column4", "name_3")
    assert table.row_count == 2


def test_encoding_detection_edge_cases():
    assert detect_encoding(b"plain ascii") == "utf-8"
    assert detect_encoding("привет".encode()[:-1]) == "utf-8"  # обрыв внутри символа
    assert detect_encoding(("Заказы клиентов, оплата " * 20).encode("cp1251")) == "cp1251"
    with pytest.raises(CsvReadError):
        detect_encoding(bytes(range(256)) * 4, "x.csv")


@pytest.mark.parametrize(
    ("file", "taken", "expected"),
    [
        ("Заказы_2024.csv", set(), "raw_zakazy_2024"),
        ("Sales Data (final).CSV", set(), "raw_sales_data_final"),
        ("щёлк.csv", set(), "raw_schyolk".replace("yo", "e")),
        ("!!!.csv", set(), "raw_table"),
        ("a.csv", {"raw_a", "raw_a_2"}, "raw_a_3"),
    ],
)
def test_raw_table_name(file, taken, expected):
    assert raw_table_name(file, taken) == expected


def test_source_details_are_stored_in_technical_table(con):
    first = load_raw_csv(con, FIXTURES / "orders_cp1251_semicolon.csv")
    second = load_raw_csv(con, FIXTURES / "customers_utf8_semicolon.csv")

    assert read_sources(con) == [first, second]
    assert con.execute(
        "SELECT raw_table, file_encoding, delimiter, row_count FROM _djedai_sources ORDER BY 1"
    ).fetchall() == [
        ("raw_customers_utf8_semicolon", "utf-8", ";", 2),
        ("raw_orders_cp1251_semicolon", "cp1251", ";", 3),
    ]


def test_failed_load_leaves_no_source_record(con):
    with pytest.raises(CsvReadError):
        load_raw_csv(con, FIXTURES / "empty.csv")

    assert read_sources(con) == []


def test_sources_survive_reconnect(tmp_path):
    database = tmp_path / "data.duckdb"
    connection = duckdb.connect(str(database))
    loaded = load_raw_csv(connection, FIXTURES / "customers_cp1251_comma.csv")
    connection.close()

    reopened = duckdb.connect(str(database))
    try:
        assert read_sources(reopened) == [loaded]
    finally:
        reopened.close()


def test_single_numeric_record_is_data_not_header(con, tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("00123,Ann\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert table.header is False
    assert table.columns == ("column1", "column2")
    assert rows(con, table.raw_table) == [("00123", "Ann")]


def test_text_file_without_header_keeps_first_record(con, tmp_path):
    path = tmp_path / "cities.csv"
    path.write_text("Moscow,Russia\nKazan,Russia\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert table.header is False
    assert rows(con, table.raw_table) == [("Kazan", "Russia"), ("Moscow", "Russia")]


@pytest.mark.parametrize(
    ("header", "expected_columns", "expected_rows"),
    [
        (True, ("Moscow", "Russia"), [("Kazan", "Belarus")]),
        (False, ("column1", "column2"), [("Kazan", "Belarus"), ("Moscow", "Russia")]),
    ],
)
def test_explicit_header_overrides_detection(
    con, tmp_path, header, expected_columns, expected_rows
):
    path = tmp_path / "ambiguous.csv"
    path.write_text("Moscow,Russia\nKazan,Belarus\n", encoding="utf-8", newline="\n")

    table = load_raw_csvs(con, [path], header=header).loaded[0]

    assert table.header is header
    assert table.columns == expected_columns
    assert rows(con, table.raw_table) == expected_rows


def test_quoted_empty_string_differs_from_missing_value(con, tmp_path):
    path = tmp_path / "quoted.csv"
    path.write_text('id,note\n001,""\n002,\n', encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert rows(con, table.raw_table) == [("001", ""), ("002", None)]


def test_single_quote_character_is_detected(con, tmp_path):
    path = tmp_path / "single.csv"
    path.write_text("id;note\n001;'hello;world'\n002;'bye;world'\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert (table.delimiter, table.quotechar) == (";", "'")
    assert rows(con, table.raw_table) == [("001", "hello;world"), ("002", "bye;world")]
    assert read_sources(con)[0].quotechar == "'"


def test_apostrophes_in_text_do_not_change_quote_character():
    assert detect_dialect("id,name\n1,O'Brien\n2,D'Arcy\n").quotechar == '"'


def test_field_longer_than_csv_default_limit_is_loaded(con, tmp_path):
    path = tmp_path / "long.csv"
    path.write_text('id,note\n001,"' + "x" * 150_000 + '"\n', encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert con.execute(f'SELECT length(note) FROM "{table.raw_table}"').fetchone() == (150_000,)


def _tables(con):
    return {row[0] for row in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}


def test_late_failure_removes_table_and_allows_clean_retry(con, monkeypatch):
    def fail(*args, **kwargs):
        raise CsvReadError("orders.csv", "сбой проверки")

    with monkeypatch.context() as patch:
        patch.setattr("djedai.data.csv_ingest._count_records", fail)
        report = load_raw_csvs(con, [FIXTURES / "orders_utf8_comma.csv"])

    assert [str(e) for e in report.errors] == ["Файл «orders.csv»: сбой проверки"]
    assert _tables(con) <= {"_djedai_sources"}
    assert read_sources(con) == []
    assert (
        load_raw_csv(con, FIXTURES / "orders_utf8_comma.csv").raw_table == "raw_orders_utf8_comma"
    )


def test_failed_source_record_removes_table(con, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("диск недоступен")

    monkeypatch.setattr("djedai.data.csv_ingest._record_source", fail)

    with pytest.raises(RuntimeError):
        load_raw_csv(con, FIXTURES / "customers_utf8_semicolon.csv")

    assert _tables(con) <= {"_djedai_sources"}


def test_unbalanced_quotes_error_names_original_file(con, tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text('id,note\n001,"abc\n', encoding="utf-8", newline="\n")

    with pytest.raises(CsvReadError) as error:
        load_raw_csv(con, path)

    assert "broken.csv" in str(error.value)
    assert "utf8.csv" not in str(error.value)
    assert _tables(con) <= {"_djedai_sources"}


def test_single_quotes_without_header_give_two_columns(con, tmp_path):
    path = tmp_path / "single_noheader.csv"
    path.write_text("001;'hello;world'\n002;'bye;world'\n", encoding="utf-8", newline="\n")

    table = load_raw_csv(con, path)

    assert (table.delimiter, table.quotechar, table.header) == (";", "'", False)
    assert table.columns == ("column1", "column2")
    assert rows(con, table.raw_table) == [("001", "hello;world"), ("002", "bye;world")]


@pytest.mark.parametrize(
    ("text", "delimiter", "quotechar"),
    [
        ("001;'a;b'\n002;'c;d'\n", ";", "'"),
        ('001;"a;b"\n002;"c;d"\n', ";", '"'),
        ("a,b\n1,2\n", ",", '"'),
        ("a,b\n1,it's\n2,o'k\n", ",", '"'),
        ("a|b\n'x|y'|1\n'z|w'|2\n", "|", "'"),
    ],
)
def test_delimiter_and_quote_detection_together(text, delimiter, quotechar):
    dialect = detect_dialect(text)

    assert (dialect.delimiter, dialect.quotechar) == (delimiter, quotechar)
