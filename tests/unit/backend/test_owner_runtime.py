"""Ownership is rechecked below HTTP, before model, result persistence or file I/O."""

from datetime import UTC, datetime
from types import SimpleNamespace

import duckdb
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from djedai.backend.chat_service import AgentRuntime, stream_chat_turn
from djedai.backend.dataset_service import DatasetContext
from djedai.backend.db.models import ChatModel, MessageModel, ToolResultModel
from djedai.backend.db.repositories import (
    _resolve_data_ref_path,
    create_chat,
    create_dataset,
    get_or_create_user,
    get_owned_tool_result,
    load_tool_result,
    save_tool_result,
)
from djedai.backend.db.session import create_db_engine, init_db
from djedai.contracts.agent import Block
from djedai.contracts.catalog import Column, DatasetCatalog, DatasetSource, Table
from djedai.contracts.tool_result import ToolResult, ToolResultData


@pytest.fixture
def db():
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        for owner in ("alice", "bob"):
            get_or_create_user(session, owner, owner)
            create_dataset(session, f"ds_{owner}", owner, owner)
            create_chat(session, f"chat_{owner}", owner, f"ds_{owner}")
        create_chat(session, "chat_alice_second", "alice", "ds_alice")
        yield session
    engine.dispose()


def result(result_id="r_owned", **kwargs):
    return ToolResult(
        result_id=result_id,
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["n"], rows=[[1]]),
        **kwargs,
    )


def test_cancelled_runtime_never_opens_source_or_calls_model(db):
    calls = []
    runtime = AgentRuntime(
        graph=SimpleNamespace(stream=lambda *a, **k: calls.append("model")),
        dataset_context=lambda *a: calls.append("dataset"),
    )
    events = list(
        stream_chat_turn(
            db, "chat_alice", "query", runtime, is_cancelled=lambda: True, owner_id="alice"
        )
    )
    assert events == []
    assert calls == []
    assert list(db.scalars(select(MessageModel))) == []


def test_runtime_does_not_derive_owner_from_supplied_chat(db):
    calls = []
    runtime = AgentRuntime(
        graph=SimpleNamespace(stream=lambda *a, **k: calls.append("model")),
        dataset_context=lambda *a: calls.append("dataset"),
    )
    events = list(stream_chat_turn(db, "chat_bob", "query", runtime, owner_id="alice"))
    assert [event.event for event in events] == ["error"]
    assert calls == []
    assert list(db.scalars(select(MessageModel))) == []


def test_runtime_rejects_persisted_chat_dataset_owner_mismatch(db):
    chat = db.get(ChatModel, "chat_alice")
    chat.dataset_id = "ds_bob"
    db.commit()
    runtime = AgentRuntime(graph=SimpleNamespace(stream=lambda *a, **k: pytest.fail("model ran")))
    events = list(stream_chat_turn(db, chat, "query", runtime, owner_id="alice"))
    assert [event.event for event in events] == ["error"]
    assert list(db.scalars(select(MessageModel))) == []


def test_runtime_does_not_fallback_to_unpersisted_chat_object(db):
    forged = ChatModel(chat_id="absent", owner_id="alice", dataset_id="ds_alice", title="fake")
    events = list(stream_chat_turn(db, forged, "query", AgentRuntime(graph=None), owner_id="alice"))
    assert [event.event for event in events] == ["error"]


@pytest.mark.parametrize("foreign", [True, False])
def test_runtime_does_not_publish_invented_or_foreign_block_references(db, tmp_path, foreign):
    if foreign:
        save_tool_result(db, result("block_ref"), chat_id="chat_bob", storage_dir=tmp_path)

    class Graph:
        def stream(self, *args, **kwargs):
            yield {"compose": {"answer_blocks": [Block(type="table", result_id="block_ref")]}}

    events = list(
        stream_chat_turn(db, "chat_alice", "query", AgentRuntime(graph=Graph()), owner_id="alice")
    )
    assert events[-1].event == "error"
    assert not any(event.event in {"block", "done"} for event in events)
    assert [message.role for message in db.scalars(select(MessageModel))] == ["user"]


def test_runtime_binds_dispatcher_to_catalog_and_closes_connection(db, tmp_path):
    path = tmp_path / "dataset.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE sales (amount INTEGER)")
        connection.execute("INSERT INTO sales VALUES (20)")
        connection.execute("CREATE TABLE raw_sales (secret VARCHAR)")
    column = Column(
        name="amount",
        original_name="amount",
        physical_type="INTEGER",
        semantic_type="integer",
        role="measure",
        null_ratio=0,
        distinct_count=1,
        is_unique=True,
    )
    catalog = DatasetCatalog(
        dataset_id="ds_alice",
        name="sales",
        created_at=datetime.now(UTC),
        source=DatasetSource(kind="csv_upload"),
        tables=[
            Table(
                table_id="sales",
                raw_table="raw_sales",
                original_name="sales",
                row_count=1,
                duplicate_rows=0,
                columns=[column],
            )
        ],
    )
    observed = []

    class Graph:
        def stream(self, state, *, config, stream_mode):
            dispatch = config["configurable"]["tool_dispatcher"]
            assert dispatch("run_sql", {"sql": "SELECT * FROM raw_sales"}).status == "error"
            data = dispatch("run_sql", {"sql": "SELECT SUM(amount) FROM sales"})
            observed.append(data.data.rows)
            yield {"execute": {"results": [data]}}
            yield {"compose": {"answer_blocks": [Block(type="table", result_id=data.result_id)]}}

    runtime = AgentRuntime(
        graph=Graph(),
        dataset_context=lambda owner, dataset: DatasetContext(catalog, path),
        results_storage_dir=tmp_path / "results",
    )
    events = list(stream_chat_turn(db, "chat_alice", "sum", runtime, owner_id="alice"))
    assert events[-1].event == "done"
    assert observed == [[[20]]]
    # Opening a writable connection fails if the read-only connection leaked.
    with duckdb.connect(str(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sales").fetchone() == (1,)


def test_truncated_dependency_fails_before_partial_aggregate(db, tmp_path):
    path = (tmp_path / "dataset.duckdb").resolve()
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE sales (amount INTEGER)")
        connection.execute("INSERT INTO sales VALUES (100), (200), (50)")
    column = Column(
        name="amount",
        original_name="amount",
        physical_type="INTEGER",
        semantic_type="integer",
        role="measure",
        null_ratio=0,
        distinct_count=3,
        is_unique=False,
    )
    catalog = DatasetCatalog(
        dataset_id="ds_alice",
        name="sales",
        created_at=datetime.now(UTC),
        source=DatasetSource(kind="csv_upload"),
        tables=[
            Table(
                table_id="sales",
                raw_table="sales",
                original_name="sales",
                row_count=3,
                duplicate_rows=0,
                columns=[column],
            )
        ],
    )
    observed: list[ToolResult] = []

    class Graph:
        def stream(self, state, *, config, stream_mode):
            dispatch = config["configurable"]["tool_dispatcher"]
            parent = ToolResult(
                result_id="r_parent",
                status="ok",
                tool="run_sql",
                data=ToolResultData(
                    columns=["amount"],
                    rows=[[100], [200]],
                    row_count=3,
                    truncated=True,
                ),
            )
            observed.append(
                dispatch(
                    "run_sql",
                    {"sql": "SELECT SUM(amount) AS total FROM source_result"},
                    source_result=parent,
                )
            )
            yield {"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}}

    runtime = AgentRuntime(
        graph=Graph(),
        dataset_context=lambda owner, dataset: DatasetContext(catalog, path),
        results_storage_dir=tmp_path / "results",
    )
    events = list(stream_chat_turn(db, "chat_alice", "dashboard", runtime, owner_id="alice"))

    assert events[-1].event == "done"
    dependent = observed[0]
    assert dependent.status == "error"
    assert dependent.data is None
    assert dependent.error is not None
    assert dependent.error.code == "dependency_truncated"
    assert "усеч" in dependent.error.message


@pytest.mark.parametrize("parent", ["missing", "r_bob", "r_other_chat", "r_audit"])
def test_saved_result_cannot_claim_foreign_missing_or_unowned_source(db, tmp_path, parent):
    save_tool_result(db, result("r_bob"), chat_id="chat_bob", storage_dir=tmp_path)
    save_tool_result(db, result("r_other_chat"), chat_id="chat_alice_second", storage_dir=tmp_path)
    save_tool_result(db, result("r_audit"), storage_dir=tmp_path)
    with pytest.raises(ValueError, match="Источник"):
        save_tool_result(
            db,
            result(source_result_id=parent),
            chat_id="chat_alice",
            owner_id="alice",
            storage_dir=tmp_path,
        )
    assert db.get(ToolResultModel, "r_owned") is None
    assert not (tmp_path / "r_owned.parquet").exists()


def test_saved_result_cannot_create_provenance_cycle_on_update(db, tmp_path):
    save_tool_result(db, result("first"), chat_id="chat_alice", storage_dir=tmp_path)
    save_tool_result(
        db, result("second", source_result_id="first"), chat_id="chat_alice", storage_dir=tmp_path
    )
    with pytest.raises(ValueError, match="Циклическое"):
        save_tool_result(
            db,
            result("first", source_result_id="second"),
            chat_id="chat_alice",
            storage_dir=tmp_path,
        )
    assert db.get(ToolResultModel, "first").source_result_id is None


@pytest.mark.parametrize("existing_owner", ["bob", None])
def test_runtime_result_id_cannot_overwrite_or_claim_existing_record(db, tmp_path, existing_owner):
    save_tool_result(db, result("collision"), owner_id=existing_owner, storage_dir=tmp_path)
    original = (tmp_path / "collision.parquet").read_bytes()
    with pytest.raises(ValueError, match="Конфликт владельцев"):
        save_tool_result(
            db, result("collision"), chat_id="chat_alice", owner_id="alice", storage_dir=tmp_path
        )
    assert (tmp_path / "collision.parquet").read_bytes() == original


def test_result_cannot_be_rebound_from_owner_only_audit_to_chat(db, tmp_path):
    save_tool_result(db, result("owner_only"), owner_id="alice", storage_dir=tmp_path)
    with pytest.raises(ValueError, match="смена чата"):
        save_tool_result(
            db, result("owner_only"), chat_id="chat_alice", owner_id="alice", storage_dir=tmp_path
        )
    assert db.get(ToolResultModel, "owner_only").chat_id is None


def test_result_file_binding_is_checked_for_writes_and_metadata_reads(db, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="expected result_id"):
        save_tool_result(
            db,
            result(data_ref="parquet://r_bob.parquet"),
            owner_id="alice",
            storage_dir=tmp_path,
            persist_data=False,
        )
    db.add(
        ToolResultModel(
            result_id="forged", owner_id="alice", status="ok", data_ref="parquet://r_bob.parquet"
        )
    )
    db.commit()
    monkeypatch.setattr(
        "djedai.backend.db.repositories.read_parquet",
        lambda *args: pytest.fail("foreign file was read"),
    )
    assert load_tool_result(db, "forged", owner_id="alice", storage_dir=tmp_path) is None
    assert load_tool_result(db, "forged", owner_id="alice", load_data=False) is None


def test_owner_scoped_result_read_checks_parent_chain_and_mixed_dataset(db, tmp_path, monkeypatch):
    record = save_tool_result(db, result(), chat_id="chat_alice", storage_dir=tmp_path)
    monkeypatch.setattr(
        "djedai.backend.db.repositories.read_parquet",
        lambda *args: pytest.fail("out-of-scope file was read"),
    )
    assert load_tool_result(db, record.result_id, owner_id="bob", storage_dir=tmp_path) is None
    record.source_result_id = "absent"
    db.commit()
    assert get_owned_tool_result(db, record.result_id, "alice") is None
    record.source_result_id = None
    db.get(ChatModel, "chat_alice").dataset_id = "ds_bob"
    db.commit()
    assert load_tool_result(db, record.result_id, owner_id="alice", storage_dir=tmp_path) is None


def test_symbolic_link_to_another_result_is_rejected_even_inside_storage(tmp_path):
    target = tmp_path / "bob.parquet"
    target.write_bytes(b"private")
    link = tmp_path / "alice.parquet"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(ValueError, match="symbolic"):
        _resolve_data_ref_path("parquet://alice.parquet", tmp_path, "alice")


def test_result_id_cannot_escape_storage_directory(db, tmp_path):
    with pytest.raises(ValueError, match="идентификатор"):
        save_tool_result(db, result("../outside"), storage_dir=tmp_path)
    assert not (tmp_path.parent / "outside.parquet").exists()
