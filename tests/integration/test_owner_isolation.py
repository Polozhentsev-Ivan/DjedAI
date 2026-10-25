"""AUTH-3 resource isolation with real HTTP accounts and persisted Bearer sessions."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.api.routes import results as results_routes
from djedai.backend.app import create_app
from djedai.backend.chat_service import AgentRuntime, get_agent_runtime
from djedai.backend.config import Settings
from djedai.backend.db.models import (
    ChatModel,
    DashboardModel,
    DatasetModel,
    SessionModel,
    ToolResultModel,
    UserModel,
)
from djedai.backend.db.repositories import get_chat_messages, save_tool_result
from djedai.backend.db.session import init_db
from djedai.contracts.agent import Block
from djedai.contracts.tool_result import ToolResult, ToolResultData

pytestmark = pytest.mark.integration
PASSWORD = "owner-isolation-password"


@dataclass(frozen=True)
class Actor:
    user_id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class OwnerAPI:
    client: TestClient
    factory: sessionmaker[Session]
    app: FastAPI
    alice: Actor
    bob: Actor


class ReplyGraph:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, *_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        self.calls += 1
        yield {"compose": {"answer_blocks": [Block(type="text", markdown="Owner answer")]}}


def _register(client: TestClient, username: str) -> Actor:
    credentials = {"username": username, "password": PASSWORD}
    registered = client.post("/api/auth/register", json=credentials)
    assert registered.status_code == 201, registered.text
    logged_in = client.post("/api/auth/login", json=credentials)
    assert logged_in.status_code == 200, logged_in.text
    actor = Actor(registered.json()["user_id"], logged_in.json()["access_token"])
    assert client.get("/api/auth/me", headers=actor.headers).json()["user_id"] == actor.user_id
    return actor


@pytest.fixture
def owner_api(tmp_path: Path) -> Iterator[OwnerAPI]:
    app = create_app(
        Settings(
            _env_file=None,
            cp1_local_mode=False,
            app_database_url=f"sqlite:///{tmp_path / 'owners.db'}",
            datasets_data_dir=str(tmp_path / "datasets"),
            results_data_dir=str(tmp_path / "results"),
            auth_rate_limit_requests=50,
        )
    )
    init_db(app.state.engine)
    app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(graph=ReplyGraph())
    with TestClient(app) as client:
        alice, bob = _register(client, "owner-alice"), _register(client, "owner-bob")
        yield OwnerAPI(client, app.state.session_factory, app, alice, bob)


def _upload(api: OwnerAPI, actor: Actor, *, amount: int = 10) -> str:
    response = api.client.post(
        "/api/datasets/upload",
        headers=actor.headers,
        files={"files": ("private.csv", f"amount\n{amount}\n".encode(), "text/csv")},
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    events = api.client.get(f"/api/datasets/{dataset_id}/events", headers=actor.headers)
    assert events.status_code == 200 and "event: done" in events.text, events.text
    return dataset_id


def _chat(api: OwnerAPI, actor: Actor, dataset_id: str) -> str:
    response = api.client.post("/api/chats", headers=actor.headers, json={"dataset_id": dataset_id})
    assert response.status_code == 201, response.text
    return response.json()["chat_id"]


def _not_found(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"
    return response.json()


def _unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"
    assert "event:" not in response.text


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=6)
        listener.close()
    assert not thread.is_alive()


def test_every_resource_route_requires_bearer_even_in_local_mode(owner_api: OwnerAPI) -> None:
    api = owner_api
    chart = {"chart_id": "chart", "result_id": "result", "type": "bar", "title": "Chart"}
    requests = [
        ("GET", "/api/datasets", {}),
        ("POST", "/api/datasets/upload", {"files": {"files": ("x.csv", b"x\n1\n")}}),
        (
            "POST",
            "/api/datasets/connect",
            {"json": {"kind": "postgresql", "dsn": "postgresql://reader:private@db/test"}},
        ),
        (
            "POST",
            "/api/datasets/connect/check",
            {"json": {"kind": "clickhouse", "dsn": "http://reader:private@db:8123/test"}},
        ),
        ("GET", "/api/datasets/missing", {}),
        ("GET", "/api/datasets/missing/catalog", {}),
        ("GET", "/api/datasets/missing/events", {}),
        ("GET", "/api/chats?dataset_id=missing", {}),
        ("POST", "/api/chats", {"json": {"dataset_id": "missing"}}),
        ("GET", "/api/chats/missing", {}),
        ("POST", "/api/chats/missing/messages", {"json": {"text": "Hello"}}),
        ("GET", "/api/results/missing", {}),
        ("GET", "/api/results/missing/data", {}),
        ("GET", "/api/dashboards?dataset_id=missing", {}),
        (
            "POST",
            "/api/dashboards",
            {"json": {"dataset_id": "missing", "result_id": "result", "chart": chart}},
        ),
        ("GET", "/api/dashboards/missing", {}),
        ("POST", "/api/dashboards/missing/refresh", {}),
        ("PATCH", "/api/dashboards/missing", {"json": {"title": "Renamed"}}),
    ]
    for local_mode in (False, True):
        api.app.state.settings.cp1_local_mode = local_mode
        api.app.state.settings.cp1_local_user_id = api.alice.user_id
        for method, path, kwargs in requests:
            response = api.client.request(method, path, **kwargs)
            _unauthorized(response)
            assert "reader:private" not in response.text


def test_two_users_have_separate_datasets_chats_and_sse(owner_api: OwnerAPI) -> None:
    api = owner_api
    alice_ds, bob_ds = _upload(api, api.alice), _upload(api, api.bob, amount=98765)
    alice_chat, bob_chat = _chat(api, api.alice, alice_ds), _chat(api, api.bob, bob_ds)
    graph = ReplyGraph()
    api.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(graph=graph)
    for actor, own_ds, own_chat, other_ds, other_chat in (
        (api.alice, alice_ds, alice_chat, bob_ds, bob_chat),
        (api.bob, bob_ds, bob_chat, alice_ds, alice_chat),
    ):
        assert [
            item["dataset_id"]
            for item in api.client.get("/api/datasets", headers=actor.headers).json()
        ] == [own_ds]
        listed = api.client.get("/api/chats", params={"dataset_id": own_ds}, headers=actor.headers)
        assert [item["chat_id"] for item in listed.json()] == [own_chat]
        assert api.client.get(f"/api/chats/{own_chat}", headers=actor.headers).status_code == 200
        for suffix in ("", "/catalog", "/events"):
            foreign = api.client.get(f"/api/datasets/{other_ds}{suffix}", headers=actor.headers)
            missing = api.client.get(f"/api/datasets/absent{suffix}", headers=actor.headers)
            assert _not_found(foreign) == _not_found(missing)
        for dataset_id in (other_ds, "absent"):
            _not_found(
                api.client.get(
                    "/api/chats", params={"dataset_id": dataset_id}, headers=actor.headers
                )
            )
            _not_found(
                api.client.post(
                    "/api/chats", json={"dataset_id": dataset_id}, headers=actor.headers
                )
            )
        assert _not_found(
            api.client.get(f"/api/chats/{other_chat}", headers=actor.headers)
        ) == _not_found(api.client.get("/api/chats/absent", headers=actor.headers))
        assert _not_found(
            api.client.post(
                f"/api/chats/{other_chat}/messages",
                json={"text": "Private?"},
                headers=actor.headers,
            )
        ) == _not_found(
            api.client.post(
                "/api/chats/absent/messages", json={"text": "Private?"}, headers=actor.headers
            )
        )
    assert graph.calls == 0
    with api.factory() as db:
        assert get_chat_messages(db, alice_chat) == get_chat_messages(db, bob_chat) == []


def test_owner_context_ignores_headers_queries_and_form_user_ids(owner_api: OwnerAPI) -> None:
    api = owner_api
    headers = {**api.alice.headers, "X-User-ID": api.bob.user_id}
    response = api.client.post(
        "/api/datasets/upload",
        headers=headers,
        params={"user_id": api.bob.user_id, "owner_id": api.bob.user_id},
        data={"user_id": api.bob.user_id, "owner_id": api.bob.user_id},
        files={"files": ("scope.csv", b"amount\n10\n", "text/csv")},
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    assert (
        "event: done"
        in api.client.get(f"/api/datasets/{dataset_id}/events", headers=api.alice.headers).text
    )
    assert (
        api.client.get(
            "/api/datasets", params={"user_id": api.bob.user_id}, headers=headers
        ).json()[0]["dataset_id"]
        == dataset_id
    )
    assert api.client.get("/api/datasets", headers=api.bob.headers).json() == []
    with api.factory() as db:
        assert db.get(DatasetModel, dataset_id).owner_id == api.alice.user_id
    for field in ("user_id", "owner_id"):
        forged = api.client.post(
            "/api/chats",
            json={"dataset_id": dataset_id, field: api.bob.user_id},
            headers=api.alice.headers,
        )
        assert forged.status_code == 422, forged.text
    chat_id = _chat(api, api.alice, dataset_id)
    forged_message = api.client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Question", "user_id": api.bob.user_id},
        headers=api.alice.headers,
    )
    assert forged_message.status_code == 422
    with api.factory() as db:
        assert get_chat_messages(db, chat_id) == []


def test_foreign_and_null_owned_result_ids_never_load_data(
    owner_api: OwnerAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = owner_api
    with api.factory() as db:
        for result_id, actor, value in (
            ("alice-result", api.alice, 10),
            ("bob-result", api.bob, 98765),
        ):
            save_tool_result(
                db,
                ToolResult(
                    result_id=result_id,
                    status="ok",
                    tool="run_sql",
                    sql="SELECT private_value FROM private_table",
                    data=ToolResultData(columns=["private_value"], rows=[[value]], row_count=1),
                ),
                owner_id=actor.user_id,
                persist_data=True,
                storage_dir=api.app.state.settings.results_data_dir,
            )
        db.add(ToolResultModel(result_id="null-result", owner_id=None, status="ok"))
        db.commit()
    assert api.client.get("/api/results/alice-result/data", headers=api.alice.headers).json()[
        "rows"
    ] == [[10]]
    assert api.client.get("/api/results/bob-result/data", headers=api.bob.headers).json()[
        "rows"
    ] == [[98765]]

    def forbidden_load(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("A foreign result must be rejected before metadata or Parquet loading")

    monkeypatch.setattr(results_routes, "load_tool_result", forbidden_load)
    for actor, foreign_id in ((api.alice, "bob-result"), (api.bob, "alice-result")):
        for suffix in ("", "/data"):
            missing = _not_found(
                api.client.get(f"/api/results/absent{suffix}", headers=actor.headers)
            )
            for result_id in (foreign_id, "null-result"):
                response = api.client.get(
                    f"/api/results/{result_id}{suffix}", headers=actor.headers
                )
                assert _not_found(response) == missing
                assert "private_table" not in response.text and "98765" not in response.text


def test_chat_pointing_at_another_owners_dataset_is_hidden(owner_api: OwnerAPI) -> None:
    api = owner_api
    foreign_ds = _upload(api, api.bob)
    with api.factory() as db:
        db.add(
            ChatModel(
                chat_id="mismatched-chat",
                owner_id=api.alice.user_id,
                dataset_id=foreign_ds,
                title="Inconsistent record",
            )
        )
        db.commit()
    _not_found(api.client.get("/api/chats/mismatched-chat", headers=api.alice.headers))
    response = api.client.post(
        "/api/chats/mismatched-chat/messages", json={"text": "Private?"}, headers=api.alice.headers
    )
    _not_found(response)
    with api.factory() as db:
        assert get_chat_messages(db, "mismatched-chat") == []


def test_result_parent_chain_cannot_cross_users(owner_api: OwnerAPI) -> None:
    api = owner_api
    with api.factory() as db:
        # A historical or corrupted record cannot make another user's parent readable.
        db.add_all(
            [
                ToolResultModel(
                    result_id="foreign-parent",
                    owner_id=api.bob.user_id,
                    status="ok",
                    sql="SELECT secret FROM bob_private",
                    data_preview={"columns": ["secret"], "rows": [[98765]], "row_count": 1},
                ),
                ToolResultModel(
                    result_id="invalid-child",
                    owner_id=api.alice.user_id,
                    status="ok",
                    source_result_id="foreign-parent",
                    sql="SELECT secret FROM bob_private",
                    data_preview={"columns": ["secret"], "rows": [[98765]], "row_count": 1},
                ),
            ]
        )
        db.commit()
    for suffix in ("", "/data"):
        response = api.client.get(f"/api/results/invalid-child{suffix}", headers=api.alice.headers)
        assert _not_found(response) == _not_found(
            api.client.get(f"/api/results/absent{suffix}", headers=api.alice.headers)
        )
        assert "bob_private" not in response.text and "98765" not in response.text


def test_dashboard_stubs_validate_ownership_before_not_implemented(owner_api: OwnerAPI) -> None:
    api = owner_api
    own_ds, foreign_ds = _upload(api, api.alice), _upload(api, api.bob)
    own_chat, foreign_chat = _chat(api, api.alice, own_ds), _chat(api, api.bob, foreign_ds)
    with api.factory() as db:
        for prefix, actor, dataset_id, chat_id in (
            ("own", api.alice, own_ds, own_chat),
            ("foreign", api.bob, foreign_ds, foreign_chat),
        ):
            db.add(
                DashboardModel(
                    dashboard_id=f"{prefix}-dashboard",
                    owner_id=actor.user_id,
                    dataset_id=dataset_id,
                    title="Private dashboard",
                    widgets=[],
                )
            )
            db.commit()
            save_tool_result(
                db,
                ToolResult(
                    result_id=f"{prefix}-result",
                    status="ok",
                    tool="run_sql",
                    data=ToolResultData(columns=["amount"], rows=[[10]], row_count=1),
                ),
                owner_id=actor.user_id,
                chat_id=chat_id,
                persist_data=False,
            )
    assert (
        api.client.get(
            "/api/dashboards", params={"dataset_id": own_ds}, headers=api.alice.headers
        ).status_code
        == 501
    )
    for dataset_id in (foreign_ds, "absent"):
        _not_found(
            api.client.get(
                "/api/dashboards", params={"dataset_id": dataset_id}, headers=api.alice.headers
            )
        )
    for method, suffix, kwargs in (
        ("GET", "", {}),
        ("POST", "/refresh", {}),
        ("PATCH", "", {"json": {"title": "Renamed"}}),
    ):
        assert (
            api.client.request(
                method,
                f"/api/dashboards/own-dashboard{suffix}",
                headers=api.alice.headers,
                **kwargs,
            ).status_code
            == 501
        )
        assert _not_found(
            api.client.request(
                method,
                f"/api/dashboards/foreign-dashboard{suffix}",
                headers=api.alice.headers,
                **kwargs,
            )
        ) == _not_found(
            api.client.request(
                method,
                f"/api/dashboards/absent{suffix}",
                headers=api.alice.headers,
                **kwargs,
            )
        )
    for result_id, status in (("own-result", 501), ("foreign-result", 404), ("absent", 404)):
        chart = {"chart_id": "chart", "result_id": result_id, "type": "bar", "title": "Chart"}
        response = api.client.post(
            "/api/dashboards",
            headers=api.alice.headers,
            json={"dataset_id": own_ds, "result_id": result_id, "chart": chart},
        )
        assert response.status_code == status, response.text


class DispatchGraph:
    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    def stream(self, _state: Any, config: dict[str, Any], **_kwargs: Any):
        result = config["configurable"]["tool_dispatcher"]("run_sql", self.params)
        yield {"execute": {"results": [result]}}
        yield {"compose": {"answer_blocks": [Block(type="text", markdown="Request processed")]}}


@pytest.mark.parametrize("attack", ["source_result_id", "foreign_database_file", "local_csv"])
def test_bound_http_runtime_rejects_foreign_results_and_file_reads(
    owner_api: OwnerAPI, tmp_path: Path, attack: str
) -> None:
    api = owner_api
    own_ds, foreign_ds = _upload(api, api.alice), _upload(api, api.bob, amount=98765)
    chat_id = _chat(api, api.alice, own_ds)
    with api.factory() as db:
        save_tool_result(
            db,
            ToolResult(
                result_id="private-source",
                status="ok",
                tool="run_sql",
                data=ToolResultData(columns=["secret"], rows=[[98765]], row_count=1),
            ),
            owner_id=api.bob.user_id,
            persist_data=True,
            storage_dir=api.app.state.settings.results_data_dir,
        )
    foreign_context = api.app.state.dataset_service.get_context(api.bob.user_id, foreign_ds)
    secret_csv = tmp_path / "private.csv"
    secret_csv.write_text("secret\nprivate-file-canary\n", encoding="utf-8")
    params = {
        "source_result_id": {"sql": "SELECT * FROM private", "source_result_id": "private-source"},
        "foreign_database_file": {
            "sql": f"ATTACH '{foreign_context.duckdb_path.as_posix()}' AS stolen (READ_ONLY)"
        },
        "local_csv": {"sql": f"SELECT * FROM read_csv('{secret_csv.as_posix()}')"},
    }[attack]
    api.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        graph=DispatchGraph(params),
        dataset_context=api.app.state.dataset_service.get_context,
        results_storage_dir=api.app.state.settings.results_data_dir,
    )
    response = api.client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Read the private data"},
        headers=api.alice.headers,
    )
    assert response.status_code == 200
    assert "98765" not in response.text and "private-file-canary" not in response.text
    if attack == "source_result_id":
        assert "event: error" in response.text
        assert "event: block" not in response.text and "event: done" not in response.text
    else:
        with api.factory() as db:
            attempted = list(
                db.scalars(select(ToolResultModel).where(ToolResultModel.chat_id == chat_id))
            )
            assert attempted and all(result.status == "error" for result in attempted)
            assert all(
                result.data_ref is None and result.data_preview is None for result in attempted
            )
    _not_found(api.client.get("/api/results/private-source/data", headers=api.alice.headers))


@pytest.mark.parametrize("invalid", ["missing", "revoked", "expired", "inactive"])
def test_invalid_session_is_rejected_before_dataset_and_chat_sse_headers(
    owner_api: OwnerAPI, invalid: str
) -> None:
    api = owner_api
    dataset_id = _upload(api, api.alice)
    chat_id = _chat(api, api.alice, dataset_id)
    if invalid == "revoked":
        assert api.client.post("/api/auth/logout", headers=api.alice.headers).status_code == 204
    elif invalid in ("expired", "inactive"):
        with api.factory() as db:
            if invalid == "inactive":
                db.get(UserModel, api.alice.user_id).is_active = False
            else:
                session = db.get(SessionModel, hashlib.sha256(api.alice.token.encode()).hexdigest())
                session.created_at = datetime.now(UTC) - timedelta(days=2)
                session.expires_at = datetime.now(UTC) - timedelta(days=1)
            db.commit()
    headers = {} if invalid == "missing" else api.alice.headers
    _unauthorized(api.client.get(f"/api/datasets/{dataset_id}/events", headers=headers))
    _unauthorized(
        api.client.post(
            f"/api/chats/{chat_id}/messages", json={"text": "No access"}, headers=headers
        )
    )
    with api.factory() as db:
        assert get_chat_messages(db, chat_id) == []


class HeldGraph:
    """Cooperative graph remains alive until the session guard cancels its worker."""

    def __init__(self) -> None:
        self.started, self.cancelled = threading.Event(), threading.Event()

    def stream(self, _state: Any, config: dict[str, Any], **_kwargs: Any):
        self.started.set()
        is_cancelled = config["configurable"]["is_cancelled"]
        deadline = time.monotonic() + 8
        while not is_cancelled():
            if time.monotonic() >= deadline:
                yield {
                    "compose": {
                        "answer_blocks": [Block(type="text", markdown="private-after-revocation")]
                    }
                }
                return
            time.sleep(0.01)
        self.cancelled.set()


def test_logout_stops_a_live_chat_and_cancels_worker(owner_api: OwnerAPI) -> None:
    api = owner_api
    dataset_id = _upload(api, api.alice)
    chat_id = _chat(api, api.alice, dataset_id)
    graph = HeldGraph()
    api.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(graph=graph)
    with _serve(api.app) as url:
        with httpx.Client(
            base_url=url, headers=api.alice.headers, trust_env=False, timeout=5
        ) as client:
            with client.stream(
                "POST", f"/api/chats/{chat_id}/messages", json={"text": "Hold"}
            ) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                received = []
                for line in lines:
                    received.append(line)
                    if line == "event: status":
                        break
                assert graph.started.wait(2)
                assert (
                    api.client.post("/api/auth/logout", headers=api.alice.headers).status_code
                    == 204
                )
                received.extend(lines)
                body = "\n".join(received)
                assert "private-after-revocation" not in body
                assert "event: done" not in body
                assert graph.cancelled.wait(2), "Session revocation must cancel the agent"
    with api.factory() as db:
        assert [message.role for message in get_chat_messages(db, chat_id)] == ["user"]
    assert api.client.get(f"/api/chats/{chat_id}", headers=api.bob.headers).status_code == 404


def test_logout_closes_live_profiling_stream_without_changing_dataset_owner(
    owner_api: OwnerAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from djedai.backend import dataset_service

    api = owner_api
    entered, release = threading.Event(), threading.Event()
    original = dataset_service.ingest_csv_dataset

    def held_ingest(*args: Any, **kwargs: Any):
        entered.set()
        assert release.wait(10), "Test must release the immutable-owner background task"
        return original(*args, **kwargs)

    monkeypatch.setattr(dataset_service, "ingest_csv_dataset", held_ingest)
    response = api.client.post(
        "/api/datasets/upload",
        headers=api.alice.headers,
        files={"files": ("held.csv", b"amount\n10\n", "text/csv")},
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    assert entered.wait(3)
    try:
        with _serve(api.app) as url:
            with httpx.Client(
                base_url=url, headers=api.alice.headers, trust_env=False, timeout=5
            ) as client:
                with client.stream("GET", f"/api/datasets/{dataset_id}/events") as stream:
                    assert stream.status_code == 200
                    lines = stream.iter_lines()
                    received = []
                    for line in lines:
                        received.append(line)
                        if line == "event: progress":
                            break
                    assert (
                        api.client.post("/api/auth/logout", headers=api.alice.headers).status_code
                        == 204
                    )
                    received.extend(lines)
                    body = "\n".join(received)
                    assert '"code":"unauthorized"' in body
                    assert "event: done" not in body
                    assert not release.is_set()
        _not_found(api.client.get(f"/api/datasets/{dataset_id}", headers=api.bob.headers))
        _unauthorized(
            api.client.get(f"/api/datasets/{dataset_id}/events", headers=api.alice.headers)
        )
    finally:
        release.set()
    # A second session belongs to the same account and can read completion.
    login = api.client.post(
        "/api/auth/login", json={"username": "owner-alice", "password": PASSWORD}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (
        "event: done" in api.client.get(f"/api/datasets/{dataset_id}/events", headers=headers).text
    )
    with api.factory() as db:
        assert db.get(DatasetModel, dataset_id).owner_id == api.alice.user_id
