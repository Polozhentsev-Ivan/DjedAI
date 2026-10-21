"""CSV pipeline shared by the HTTP adapter and local integrations.

The file database is published only after every uploaded table has a typed view
and profile. Source adapters for PostgreSQL/ClickHouse remain separate stages.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from djedai.contracts.catalog import DatasetCatalog
from djedai.data.catalog_builder import build_csv_catalog
from djedai.data.csv_ingest import load_raw_csv
from djedai.data.profiler import profile_table
from djedai.data.typed_views import create_typed_view


@dataclass(frozen=True)
class CsvSource:
    path: Path
    original_name: str


def ingest_csv_dataset(
    sources: Sequence[CsvSource],
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    progress: Callable[[str, float, str], None] | None = None,
) -> DatasetCatalog:
    """Build a complete multiple-table dataset in a caller-owned database file.

    Failures propagate to the caller; it must not publish the partial database.
    No connection is shared between workers or retained after this call.
    """
    if not sources:
        raise ValueError("Загрузите хотя бы один CSV-файл")
    tables = []
    transformations = []
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database_path)) as connection:
        for index, source in enumerate(sources):
            if progress:
                progress("read", index / len(sources), "Чтение CSV")
            raw = load_raw_csv(connection, source.path, source.original_name)
            if progress:
                progress("types", (index * 3 + 1) / (len(sources) * 3), "Определение типов")
            view = create_typed_view(connection, raw)
            if progress:
                progress("profile", (index * 3 + 2) / (len(sources) * 3), "Профилирование таблицы")
            profile = profile_table(connection, view)
            partial = build_csv_catalog(connection, raw, view, profile, dataset_id=dataset_id)
            tables.extend(partial.tables)
            transformations.extend(partial.transformations)
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "csv_upload"},
        tables=tables,
        transformations=transformations,
    )
