import duckdb
import pytest


@pytest.fixture
def conn():
    """9 «нормальных» значений 10..12 и один явный выброс 100."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE metrics (id INT, value DOUBLE)")
    rows = [(i, 10 + (i % 3)) for i in range(1, 10)] + [(10, 100.0)]
    # id=1..9: value ∈ {10, 11, 12}; id=10: value=100.0
    con.executemany("INSERT INTO metrics VALUES (?, ?)", rows)
    return con
