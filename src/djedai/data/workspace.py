"""Рабочие пространства датасетов: отдельный файл DuckDB на датасет владельца (B1-8).

Файл лежит в ``<root>/<owner_id>/<dataset_id>.duckdb`` и хранит сырые таблицы,
представления, реестры, профили и связи одного датасета. Владелец записан и в пути, и в
служебной таблице ``_djedai_dataset`` внутри файла; ``open_workspace`` сверяет оба.
«Датасета нет» и «датасет чужой» неразличимы: ``DatasetAccessError`` с одним сообщением.

``owner_id`` должен приходить из проверенной сессии (AUTH-3, #103), а не из запроса
клиента или текста модели. Подключения к внешним БД (ATTACH и секрет с паролем) в файле
не сохраняются: их повторяют в каждом соединении, открытом владельцем.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

META_TABLE = "_djedai_dataset"
SOURCE_KINDS = ("csv_upload", "postgres", "clickhouse")  # как SourceKind в DatasetCatalog
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class DatasetAccessError(Exception):
    """Датасета нет или он принадлежит другому пользователю; причины не различаются."""

    def __init__(self) -> None:
        super().__init__("Датасет не найден или недоступен")


@dataclass(frozen=True)
class WorkspaceInfo:
    dataset_id: str
    owner_id: str
    source_kind: str
    created_at: datetime


def create_workspace(
    root: str | Path, owner_id: str, source_kind: str
) -> tuple[str, duckdb.DuckDBPyConnection]:
    """Создаёт файл нового датасета владельца; возвращает ``dataset_id`` и соединение."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"неизвестный тип источника: {source_kind}")
    dataset_id = f"ds_{uuid.uuid4().hex}"
    path = _path(root, owner_id, dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            f"""CREATE TABLE {META_TABLE} (
                dataset_id VARCHAR, owner_id VARCHAR, source_kind VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp)"""
        )
        connection.execute(
            f"INSERT INTO {META_TABLE} (dataset_id, owner_id, source_kind) VALUES (?, ?, ?)",
            [dataset_id, owner_id, source_kind],
        )
    except Exception:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    return dataset_id, connection


def open_workspace(
    root: str | Path, owner_id: str, dataset_id: str, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Открывает датасет владельца; чужой или несуществующий даёт ``DatasetAccessError``."""
    try:
        path = _path(root, owner_id, dataset_id)
    except ValueError:
        raise DatasetAccessError() from None
    if not path.is_file():
        raise DatasetAccessError()
    connection = duckdb.connect(str(path), read_only=read_only)
    try:
        info = read_workspace_info(connection)
    except duckdb.Error:
        info = None
    if info is None or info.owner_id != owner_id or info.dataset_id != dataset_id:
        connection.close()
        raise DatasetAccessError()
    return connection


def read_workspace_info(connection: duckdb.DuckDBPyConnection) -> WorkspaceInfo | None:
    """Сведения о датасете из открытого файла; ``None``, если файл — не рабочее пространство."""
    row = connection.execute(
        f"SELECT dataset_id, owner_id, source_kind, created_at FROM {META_TABLE}"
    ).fetchone()
    return WorkspaceInfo(*row) if row else None


def _path(root: str | Path, owner_id: str, dataset_id: str) -> Path:
    """Путь к файлу; идентификаторы — только ``[A-Za-z0-9_-]``, поэтому выйти из ``root``
    через ``..`` или абсолютный путь нельзя."""
    for value in (owner_id, dataset_id):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("недопустимый идентификатор")
    return Path(root) / owner_id / f"{dataset_id}.duckdb"
