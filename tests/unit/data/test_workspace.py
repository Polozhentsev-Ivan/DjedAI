"""Рабочие пространства датасетов (B1-8): владелец видит только свои файлы."""

from __future__ import annotations

import duckdb
import pytest

from djedai.data.workspace import (
    META_TABLE,
    DatasetAccessError,
    create_workspace,
    open_workspace,
    read_workspace_info,
)


def test_create_and_open(tmp_path):
    dataset_id, con = create_workspace(tmp_path, "alice", "postgres")
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()
    assert (tmp_path / "alice" / f"{dataset_id}.duckdb").is_file()

    with open_workspace(tmp_path, "alice", dataset_id) as reopened:
        info = read_workspace_info(reopened)
        assert (info.dataset_id, info.owner_id, info.source_kind) == (
            dataset_id, "alice", "postgres",
        )  # fmt: skip
        assert reopened.execute("SELECT a FROM t").fetchall() == [(1,)]


def test_other_user_gets_same_error_as_for_missing(tmp_path):
    dataset_id, con = create_workspace(tmp_path, "alice", "csv_upload")
    con.close()
    with pytest.raises(DatasetAccessError) as foreign:
        open_workspace(tmp_path, "bob", dataset_id)
    with pytest.raises(DatasetAccessError) as missing:
        open_workspace(tmp_path, "bob", "ds_missing")
    assert str(foreign.value) == str(missing.value) == "Датасет не найден или недоступен"


def test_owner_inside_file_is_checked(tmp_path):
    """Файл, перенесённый в чужой каталог, не открывается: владелец сверяется и внутри."""
    dataset_id, con = create_workspace(tmp_path, "alice", "postgres")
    con.close()
    (tmp_path / "bob").mkdir()
    (tmp_path / "alice" / f"{dataset_id}.duckdb").rename(tmp_path / "bob" / f"{dataset_id}.duckdb")
    with pytest.raises(DatasetAccessError):
        open_workspace(tmp_path, "bob", dataset_id)


def test_file_without_meta_is_not_a_workspace(tmp_path):
    (tmp_path / "alice").mkdir()
    duckdb.connect(str(tmp_path / "alice" / "ds_plain.duckdb")).close()
    with pytest.raises(DatasetAccessError):
        open_workspace(tmp_path, "alice", "ds_plain")


@pytest.mark.parametrize(
    ("owner_id", "dataset_id"),
    [
        ("alice", "../bob/ds_x"),
        ("..", "ds_x"),
        ("alice", "/etc/passwd"),
        ("", "ds_x"),
        ("a" * 65, "x"),
    ],
)
def test_ids_cannot_escape_root(tmp_path, owner_id, dataset_id):
    with pytest.raises(DatasetAccessError):
        open_workspace(tmp_path, owner_id, dataset_id)


def test_read_only_open(tmp_path):
    dataset_id, con = create_workspace(tmp_path, "alice", "postgres")
    con.close()
    with open_workspace(tmp_path, "alice", dataset_id, read_only=True) as reopened:
        with pytest.raises(duckdb.Error):
            reopened.execute(f"DELETE FROM {META_TABLE}")


def test_unknown_source_kind(tmp_path):
    with pytest.raises(ValueError, match="mysql"):
        create_workspace(tmp_path, "alice", "mysql")
