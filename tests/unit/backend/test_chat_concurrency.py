"""Regression coverage for database waits during a live chat request."""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.api.routes.chats import router
from djedai.backend.chat_service import AgentRuntime, get_agent_runtime
from djedai.backend.db.repositories import (
    create_chat,
    create_dataset,
    get_or_create_user,
)
from djedai.backend.db.session import create_db_engine, get_db, get_session_factory, init_db
from djedai.contracts.agent import Block


class _ReplyGraph:
    def stream(self, *_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        yield {"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}


def test_locked_chat_lookup_does_not_block_parallel_probe(tmp_path: Path) -> None:
    """A SQLite lock may delay a chat, but must leave the ASGI loop responsive."""
    database_path = tmp_path / "locked-chat.db"
    engine = create_db_engine(f"sqlite:///{database_path}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        get_or_create_user(db, "u_lock", "lock_owner")
        create_dataset(db, "ds_lock", "u_lock", "Lock test")
        create_chat(db, "c_lock", "u_lock", "ds_lock")

    lookup_started = threading.Event()

    @event.listens_for(engine, "before_cursor_execute")
    def observe_lookup(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM chats" in statement:
            lookup_started.set()

    def database_session() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = database_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(graph=_ReplyGraph())

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    post_finished = threading.Event()
    post_results: list[httpx.Response | Exception] = []

    def send_message() -> None:
        try:
            with httpx.Client(base_url=base_url, trust_env=False, timeout=5.0) as client:
                post_results.append(
                    client.post("/api/chats/c_lock/messages", json={"text": "Hello"})
                )
        except Exception as exc:
            post_results.append(exc)
        finally:
            post_finished.set()

    post_thread = threading.Thread(target=send_message, daemon=True)
    lock = sqlite3.connect(database_path)
    server_thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn did not start within five seconds"

        with httpx.Client(base_url=base_url, trust_env=False, timeout=0.75) as client:
            assert client.get("/probe").json() == {"ok": True}
            lock.execute("BEGIN EXCLUSIVE")
            post_thread.start()
            assert lookup_started.wait(2.0), "POST did not reach the chat lookup"
            assert not post_finished.is_set(), "The exclusive lock must hold the POST open"

            # Keep the real database lock held throughout the independent network request.
            # On the regression, cursor.execute blocks the event loop and this times out.
            response = client.get("/probe")
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert not post_finished.is_set()
    finally:
        lock.rollback()
        lock.close()
        if post_thread.ident is not None:
            post_thread.join(timeout=6.0)
        server.should_exit = True
        server_thread.join(timeout=6.0)
        listener.close()
        engine.dispose()

    assert not post_thread.is_alive(), "Chat request failed to finish after releasing the lock"
    assert not server_thread.is_alive(), "Uvicorn failed to shut down"
    assert len(post_results) == 1
    result = post_results[0]
    assert isinstance(result, httpx.Response), repr(result)
    assert result.status_code == 200
    assert "event: done" in result.text
