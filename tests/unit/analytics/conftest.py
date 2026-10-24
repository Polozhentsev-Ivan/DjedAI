from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from djedai.data.service import CsvSource, ingest_csv_dataset

METRIC_FIXTURES = Path(__file__).parents[2] / "fixtures" / "metrics"


@pytest.fixture
def conn():
    """9 «нормальных» значений 10..12 и один явный выброс 100."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE metrics (id INT, value DOUBLE)")
    rows = [(i, 10 + (i % 3)) for i in range(1, 10)] + [(10, 100.0)]
    # id=1..9: value ∈ {10, 11, 12}; id=10: value=100.0
    con.executemany("INSERT INTO metrics VALUES (?, ?)", rows)
    return con


@pytest.fixture(scope="session")
def metrics_dataset(tmp_path_factory):
    """orders/items/payments из ``tests/fixtures/metrics``: соединение и каталог (B2-9)."""
    path = tmp_path_factory.mktemp("metrics") / "dataset.duckdb"
    catalog = ingest_csv_dataset(
        [CsvSource(p, p.name) for p in sorted(METRIC_FIXTURES.glob("*.csv"))],
        path,
        dataset_id="ds_metrics",
        name="metrics",
        created_at=datetime.now(UTC),
    )
    with duckdb.connect(str(path), read_only=True) as connection:
        yield connection, catalog
