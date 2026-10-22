"""Concurrent claims must precede publication into the shared result store."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread, local
from uuid import UUID

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from djedai.analytics import tools
from djedai.backend.app import create_app
from djedai.backend.auth.storage import create_session, create_user
from djedai.backend.config import Settings
from djedai.backend.db import repositories
from djedai.backend.db.models import ToolResultModel
from djedai.backend.db.session import init_db


def private_result(owner):
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE sales(private VARCHAR)")
        connection.execute("INSERT INTO sales VALUES (?)", [f"{owner}-private"])
        result = tools.run_sql(
            "SELECT private FROM sales", connection=connection, allowed_tables={"sales"}
        )
    assert result.status == "ok"
    return result


def test_results_with_same_uuid_prefix_keep_distinct_identifiers(monkeypatch):
    values = iter(
        [
            UUID("01234567-1111-4111-8111-111111111111"),
            UUID("01234567-2222-4222-8222-222222222222"),
        ]
    )
    with monkeypatch.context() as patch:
        patch.setattr(tools.uuid, "uuid4", lambda: next(values))
        alice, bob = private_result("alice"), private_result("bob")
    assert alice.result_id != bob.result_id


@pytest.mark.parametrize("with_chat", [True, False])
def test_concurrent_result_collision_cannot_publish_losing_owners_rows(
    tmp_path, monkeypatch, with_chat
):
    app = create_app(
        Settings(
            _env_file=None,
            app_database_url=f"sqlite:///{tmp_path / 'app.db'}",
            datasets_data_dir=str(tmp_path / "datasets"),
            results_data_dir=str(tmp_path / "results"),
        )
    )
    init_db(app.state.engine)
    factory = app.state.session_factory
    owners, tokens = {}, {}
    with factory() as db:
        for owner in ("alice", "bob"):
            user = create_user(db, owner, "concurrent-results-password")
            owners[owner] = user.id
            tokens[owner] = create_session(db, user.id)
            repositories.create_dataset(db, f"ds_{owner}", user.id, owner)
            repositories.create_chat(db, f"chat_{owner}", user.id, f"ds_{owner}")

    result_id = "r_forced_collision"
    results = {
        owner: private_result(owner).model_copy(update={"result_id": result_id}) for owner in owners
    }
    both_checked = Barrier(2)
    alice_written, bob_attempted, alice_committed = Event(), Event(), Event()
    checked = local()
    writes = []
    original_write = repositories.write_parquet

    @event.listens_for(app.state.engine, "after_cursor_execute")
    def synchronize_absent_reads(_conn, _cursor, statement, _params, _context, _many):
        if (
            current_thread().name.startswith("result-")
            and not getattr(checked, "done", False)
            and statement.startswith("SELECT")
            and "FROM results" in statement
        ):
            checked.done = True
            # Both requests observed the result as absent, before either claims it.
            both_checked.wait(timeout=10)

    @event.listens_for(app.state.engine, "before_cursor_execute")
    def order_claims(_conn, _cursor, statement, _params, _context, _many):
        if current_thread().name == "result-bob" and statement.startswith("INSERT INTO results"):
            assert alice_written.wait(10)
            bob_attempted.set()

    def controlled_write(path, columns, rows, columns_meta=None):
        if rows == [["bob-private"]]:
            assert alice_written.wait(10)
        output = original_write(path, columns, rows, columns_meta)
        writes.append(rows)
        if rows == [["alice-private"]]:
            alice_written.set()
            assert bob_attempted.wait(10)
        else:
            # On the broken implementation this overwrites Alice's file before
            # her INSERT; Bob subsequently loses the database uniqueness check.
            bob_attempted.set()
            assert alice_committed.wait(10)
        return output

    monkeypatch.setattr(repositories, "write_parquet", controlled_write)

    def save(owner):
        current_thread().name = f"result-{owner}"
        try:
            with factory() as db:
                repositories.save_tool_result(
                    db,
                    results[owner],
                    owner_id=owners[owner],
                    chat_id=f"chat_{owner}" if with_chat else None,
                    storage_dir=tmp_path / "results",
                )
            return "saved"
        except (ValueError, IntegrityError):
            return "rejected"
        finally:
            if owner == "alice":
                alice_committed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(save, "alice"), executor.submit(save, "bob")
        assert first.result(timeout=20) == "saved"
        assert second.result(timeout=20) == "rejected"

    with factory() as db:
        assert db.get(ToolResultModel, result_id).owner_id == owners["alice"]
    with TestClient(app) as client:
        response = client.get(
            f"/api/results/{result_id}/data",
            headers={"Authorization": f"Bearer {tokens['alice']}"},
        )
        assert response.status_code == 200
        assert response.json()["rows"] == [["alice-private"]]
        assert (
            client.get(
                f"/api/results/{result_id}/data",
                headers={"Authorization": f"Bearer {tokens['bob']}"},
            ).status_code
            == 404
        )
    assert writes == [[["alice-private"]]]


def test_failed_parquet_write_rolls_back_result_claim(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, app_database_url=f"sqlite:///{tmp_path / 'failure.db'}")
    )
    init_db(app.state.engine)
    try:
        result = private_result("alice")

        def fail(*args, **kwargs):
            raise OSError("controlled disk failure")

        with app.state.session_factory() as db:
            monkeypatch.setattr(repositories, "write_parquet", fail)
            with pytest.raises(OSError, match="controlled disk failure"):
                repositories.save_tool_result(db, result, storage_dir=tmp_path / "results")
            # A caller's later commit cannot accidentally publish a partial claim.
            db.commit()
            assert db.get(ToolResultModel, result.result_id) is None
    finally:
        app.state.engine.dispose()


def test_failed_commit_leaves_no_adoptable_reference_for_another_owner(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, app_database_url=f"sqlite:///{tmp_path / 'commit-failure.db'}")
    )
    init_db(app.state.engine)
    factory = app.state.session_factory
    directory = tmp_path / "results"
    try:
        with factory() as db:
            for owner in ("alice", "bob"):
                repositories.get_or_create_user(db, owner, owner)
        result = private_result("alice")

        def fail():
            raise OSError("controlled commit failure")

        with factory() as db, monkeypatch.context() as patch:
            patch.setattr(db, "commit", fail)
            with pytest.raises(OSError, match="controlled commit failure"):
                repositories.save_tool_result(db, result, owner_id="alice", storage_dir=directory)
            assert db.get(ToolResultModel, result.result_id) is None
        orphan = directory / f"{result.result_id}.parquet"
        assert orphan.is_file()
        original = orphan.read_bytes()
        borrowed = result.model_copy(
            update={"data": None, "data_ref": f"parquet://{result.result_id}.parquet"}
        )
        with factory() as db:
            with pytest.raises(ValueError, match="присвоить файл"):
                repositories.save_tool_result(
                    db, borrowed, owner_id="bob", persist_data=False, storage_dir=directory
                )
            assert db.get(ToolResultModel, result.result_id) is None
        assert orphan.read_bytes() == original
        # A fresh computation may reuse an unclaimed ID only with its own rows.
        bob = private_result("bob").model_copy(update={"result_id": result.result_id})
        with factory() as db:
            repositories.save_tool_result(db, bob, owner_id="bob", storage_dir=directory)
            loaded = repositories.load_tool_result(
                db, result.result_id, owner_id="bob", storage_dir=directory
            )
            assert loaded.data.rows == [["bob-private"]]
            assert repositories.load_tool_result(db, result.result_id, owner_id="alice") is None
    finally:
        app.state.engine.dispose()


def test_existing_owned_reference_can_be_preserved_without_rewriting_file(tmp_path):
    app = create_app(
        Settings(_env_file=None, app_database_url=f"sqlite:///{tmp_path / 'existing.db'}")
    )
    init_db(app.state.engine)
    try:
        with app.state.session_factory() as db:
            repositories.get_or_create_user(db, "alice", "alice")
            result = private_result("alice")
            saved = repositories.save_tool_result(
                db, result, owner_id="alice", storage_dir=tmp_path
            )
            existing = result.model_copy(update={"data": None, "data_ref": saved.data_ref})
            repositories.save_tool_result(
                db, existing, owner_id="alice", persist_data=False, storage_dir=tmp_path
            )
            loaded = repositories.load_tool_result(
                db, result.result_id, owner_id="alice", storage_dir=tmp_path
            )
            assert loaded.data.rows == [["alice-private"]]
    finally:
        app.state.engine.dispose()
