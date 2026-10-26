"""Равные частоты дают один и тот же результат: состав top_n не зависит от порядка хэш-агрегации.

На HK_Datasets (09_Лекарственные_препараты) пять типов лекарств по 146 000 строк;
без порядка по значению каждый запуск показывал другой набор (#49).
"""

import random

import duckdb
import pytest

from djedai.analytics.tools import describe_column, find_duplicates, segment_profile, value_counts

CODES = [f"К-{index:04d}" for index in range(2000)]


@pytest.fixture
def tied() -> duckdb.DuckDBPyConnection:
    rows = [(code, 1) for code in CODES for _ in range(2)]
    random.Random(49).shuffle(rows)
    connection = duckdb.connect(":memory:")
    connection.execute("SET threads = 4")
    connection.execute("CREATE TABLE tied (code VARCHAR, amount INTEGER)")
    connection.executemany("INSERT INTO tied VALUES (?, ?)", rows)
    return connection


def test_value_counts_breaks_ties_by_value(tied):
    result = value_counts("tied", "code", top_n=5, connection=tied)
    assert [row[0] for row in result.data.rows] == CODES[:5]


def test_describe_column_frequencies_break_ties_by_value(tied):
    result = describe_column("tied", "code", connection=tied)
    assert [row[0] for row in result.data.rows][:10] == CODES[:10]


def test_find_duplicates_breaks_ties_by_key(tied):
    result = find_duplicates("tied", ["code"], limit=5, connection=tied)
    assert [row[0] for row in result.data.rows] == CODES[:5]


def test_segment_profile_breaks_ties_by_segment(tied):
    result = segment_profile("tied", ["code"], top_n=5, connection=tied)
    assert [row[0] for row in result.data.rows] == CODES[:5]
