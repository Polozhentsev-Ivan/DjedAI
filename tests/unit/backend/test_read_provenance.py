"""Происхождение чтения результата: хранение, «версия неизвестна» и миграция (B1-11)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from djedai.backend.db.models import ToolResultModel
from djedai.backend.db.repositories import load_tool_result, save_tool_result
from djedai.backend.db.session import create_db_engine, init_db
from djedai.contracts.freshness import ReadProvenance
from djedai.contracts.tool_result import ToolResult, ToolResultColumn, ToolResultData

ROOT = Path(__file__).resolve().parents[3]
READ = ReadProvenance.model_validate(
    {
        "revision_known": True,
        "dataset_id": "ds_1",
        "catalog_revision": 3,
        "schema_fingerprint": "a" * 64,
        "source_fingerprint": "b" * 64,
        "transform_version": "2026.10.1",
        "read_mode": "live",
        "consistency": "turn_snapshot",
        "snapshot_id": "run_1:1",
        "started_at": "2026-10-26T10:00:00Z",
        "finished_at": "2026-10-26T10:00:01Z",
        "freshness": {"state": "fresh", "profiled_at": "2026-10-26T09:00:00Z"},
    }
)


def _result(read: ReadProvenance | None = READ) -> ToolResult:
    return ToolResult(
        result_id="r_1",
        status="ok",
        tool="run_sql",
        sql="SELECT 1 AS n",
        data=ToolResultData(
            columns=[ToolResultColumn(name="n", type="integer")], rows=[[1]], row_count=1
        ),
        read=read,
    )


def test_read_survives_storage_and_is_never_erased(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as db:
        save_tool_result(db, _result(), storage_dir=tmp_path)
        assert load_tool_result(db, "r_1", storage_dir=tmp_path).read == READ
        # Re-saving the same result without provenance keeps what it once read.
        save_tool_result(db, _result(read=None), storage_dir=tmp_path)
        assert load_tool_result(db, "r_1", storage_dir=tmp_path).read == READ


def test_result_without_read_loads_as_explicit_unknown(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as db:
        save_tool_result(db, _result(read=None), storage_dir=tmp_path)
        assert db.get(ToolResultModel, "r_1").read is None
        read = load_tool_result(db, "r_1", storage_dir=tmp_path).read
    assert read == ReadProvenance.unknown()
    assert (read.revision_known, read.read_mode) == (False, "unknown")


def test_migration_marks_old_results_unknown_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "f169rev0001")
    engine = create_db_engine(url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO results (result_id, tool, status, created_at) "
                    "VALUES ('old', 'run_sql', 'ok', '2026-10-20 10:00:00')"
                )
            )
        command.upgrade(cfg, "head")
        with Session(engine) as db:
            # The migration's frozen value is exactly the current contract's unknown.
            assert db.get(ToolResultModel, "old").read == ReadProvenance.unknown().model_dump(
                mode="json"
            )
            assert load_tool_result(db, "old", storage_dir=tmp_path).read.revision_known is False
        command.downgrade(cfg, "f169rev0001")
        with engine.connect() as db:
            columns = {row[1] for row in db.execute(text("PRAGMA table_info(results)"))}
            assert "read" not in columns
            assert db.scalar(text("SELECT tool FROM results")) == "run_sql"
    finally:
        engine.dispose()
