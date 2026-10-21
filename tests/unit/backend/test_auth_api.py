"""HTTP-проверки AUTH-2 с настоящими SQLite-пользователями и сессиями."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Message, Receive, Scope, Send

from djedai.backend.api.routes import auth as auth_routes
from djedai.backend.app import create_app
from djedai.backend.auth import create_session, hash_password, storage, verify_password
from djedai.backend.auth import dependencies as auth_dependencies
from djedai.backend.auth import passwords as password_tools
from djedai.backend.auth import policy as auth_policy
from djedai.backend.auth.dependencies import current_owner_id
from djedai.backend.config import Settings
from djedai.backend.db.models import SessionModel, UserModel
from djedai.backend.db.session import init_db
from djedai.contracts.auth import TokenResponse, UserRead
from djedai.contracts.common import ErrorResponse

_PASSWORD = " пароль с пробелами 123 "
_USER_ID = "auth-api-alice"
_USER_KEYS = {"user_id", "username", "email", "is_active", "created_at"}
AuthAPI = tuple[TestClient, sessionmaker[Session], FastAPI]


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(_PASSWORD)


@pytest.fixture
def auth_api(tmp_path: Path, password_hash: str) -> Iterator[AuthAPI]:
    app = create_app(
        Settings(
            _env_file=None,
            app_database_url=f"sqlite:///{tmp_path / 'auth.db'}",
            datasets_data_dir=str(tmp_path / "datasets"),
            results_data_dir=str(tmp_path / "results"),
            cp1_local_mode=False,
            auth_rate_limit_requests=10,
            auth_rate_limit_window_seconds=300,
        )
    )
    init_db(app.state.engine)
    factory = app.state.session_factory
    with factory() as db:
        db.add(
            UserModel(
                id=_USER_ID,
                username="alice",
                email="alice@example.org",
                password_hash=password_hash,
            )
        )
        db.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory, app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(factory: sessionmaker[Session]) -> str:
    with factory() as db:
        return create_session(db, _USER_ID)


def _no_store(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def _error(response: httpx.Response, status: int, code: str) -> ErrorResponse:
    assert response.status_code == status, response.text
    result = ErrorResponse.model_validate(response.json())
    assert result.error.code == code
    assert result.error.message
    _no_store(response)
    return result


def _unauthorized(response: httpx.Response) -> dict[str, Any]:
    _error(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    return response.json()


def _all_auth_slots_available(limiter: auth_policy.AuthRateLimiter) -> None:
    acquired = 0
    try:
        for _ in range(4):
            assert limiter._slots.acquire(blocking=False)
            acquired += 1
        assert not limiter._slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            limiter._slots.release()


def test_registration_persists_hash_without_session_or_secret_response(
    auth_api: AuthAPI, caplog: pytest.LogCaptureFixture
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    response = client.post(
        "/api/auth/register",
        json={"username": "новый_логин", "password": _PASSWORD, "email": "new@example.org"},
    )
    assert response.status_code == 201, response.text
    user = UserRead.model_validate(response.json())
    assert set(response.json()) == _USER_KEYS
    assert user.user_id != _USER_ID
    assert user.username == "новый_логин"
    assert user.email == "new@example.org"
    assert user.is_active
    assert user.created_at.utcoffset() == timedelta(0)
    _no_store(response)
    assert "set-cookie" not in response.headers
    with factory() as db:
        saved = db.get(UserModel, user.user_id)
        assert saved is not None
        assert verify_password(_PASSWORD, saved.password_hash)
        assert saved.password_hash.startswith("$argon2id$")
        assert saved.password_hash not in response.text
        assert saved.password_hash not in caplog.text
        assert db.scalars(select(SessionModel)).all() == []
    assert _PASSWORD not in response.text
    assert _PASSWORD not in caplog.text
    _unauthorized(client.get("/api/auth/me"))


def test_duplicate_registration_is_409_and_preserves_original_account(
    auth_api: AuthAPI, caplog: pytest.LogCaptureFixture, password_hash: str
) -> None:
    client, factory, _ = auth_api
    secret = "duplicate-secret-password"
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/auth/register", json={"username": "alice", "password": secret})
    _error(response, 409, "conflict")
    assert secret not in response.text + caplog.text
    assert password_hash not in response.text + caplog.text
    assert "INSERT INTO" not in response.text
    assert "IntegrityError" not in response.text
    with factory() as db:
        assert len(db.scalars(select(UserModel)).all()) == 1
        assert db.get(UserModel, _USER_ID).password_hash == password_hash
        assert db.scalars(select(SessionModel)).all() == []
    assert (
        client.post(
            "/api/auth/login", json={"username": "alice", "password": _PASSWORD}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"username": "aa"},
        {"username": "a" * 51},
        {"username": "   "},
        {"password": "short"},
        {"password": "secret" * 171},
        {"email": "private" * 37},
        {"user_id": "client-selected-owner"},
        {"password": None},
    ],
)
def test_registration_validation_is_sanitized(auth_api: AuthAPI, changes: dict[str, Any]) -> None:
    client, factory, _ = auth_api
    payload = {"username": "new-user", "password": "private-validation-password", **changes}
    response = client.post("/api/auth/register", json=payload)
    _error(response, 422, "invalid_request")
    for value in payload.values():
        if isinstance(value, str) and len(value) > 10:
            assert value not in response.text
    with factory() as db:
        assert len(db.scalars(select(UserModel)).all()) == 1
        assert db.scalars(select(SessionModel)).all() == []


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "aa", "password": "valid-password"},
        {"username": "a" * 51, "password": "valid-password"},
        {"username": "   ", "password": "valid-password"},
        {"username": "alice", "password": "short"},
        {"username": "alice", "password": "password-secret" * 74},
        {"username": "alice"},
        {"username": "alice", "password": "valid-password", "owner_id": "secret-owner"},
    ],
)
def test_login_validation_rejects_unbounded_or_extra_input(
    auth_api: AuthAPI, payload: dict[str, Any]
) -> None:
    client, factory, _ = auth_api
    response = client.post("/api/auth/login", json=payload)
    _error(response, 422, "invalid_request")
    assert "valid-password" not in response.text
    assert "password-secret" not in response.text
    with factory() as db:
        assert db.scalars(select(SessionModel)).all() == []


def test_login_returns_one_time_opaque_token_and_me_resolves_persisted_user(
    auth_api: AuthAPI, caplog: pytest.LogCaptureFixture
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/auth/login", json={"username": "alice", "password": _PASSWORD})
    assert response.status_code == 200, response.text
    token = TokenResponse.model_validate(response.json())
    assert set(response.json()) == {"access_token", "token_type", "expires_in"}
    assert token.token_type == "bearer"
    assert token.expires_in == 86_400
    assert len(token.access_token) >= 43
    assert "set-cookie" not in response.headers
    _no_store(response)
    digest = hashlib.sha256(token.access_token.encode()).hexdigest()
    with factory() as db:
        rows = db.scalars(select(SessionModel)).all()
        assert len(rows) == 1
        session = rows[0]
        assert session.token_hash == digest
        assert session.user_id == _USER_ID
        assert session.revoked_at is None
        assert session.expires_at - session.created_at == timedelta(seconds=token.expires_in)
        assert token.access_token not in repr(db.execute(SessionModel.__table__.select()).all())
    me = client.get("/api/auth/me", headers=_bearer(token.access_token))
    assert me.status_code == 200, me.text
    assert set(me.json()) == _USER_KEYS
    assert UserRead.model_validate(me.json()).user_id == _USER_ID
    assert me.json()["username"] == "alice"
    _no_store(me)
    assert token.access_token not in me.text + caplog.text
    assert _PASSWORD not in response.text + me.text + caplog.text


@pytest.mark.parametrize("failure", ["unknown", "wrong-password", "inactive", "legacy", "bad-hash"])
def test_login_failures_have_the_same_generic_401(
    auth_api: AuthAPI,
    failure: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    baseline = _unauthorized(
        client.post("/api/auth/login", json={"username": "unknown", "password": _PASSWORD})
    )
    dummy_calls: list[str] = []
    original_dummy_verify = password_tools._dummy_verify

    def dummy_spy(password: str) -> None:
        dummy_calls.append(password)
        original_dummy_verify(password)

    monkeypatch.setattr(password_tools, "_dummy_verify", dummy_spy)
    username, password = "alice", _PASSWORD
    if failure == "unknown":
        username = "unknown"
    elif failure == "wrong-password":
        password = "incorrect-password"
    else:
        with factory() as db:
            user = db.get(UserModel, _USER_ID)
            if failure == "inactive":
                user.is_active = False
            else:
                user.password_hash = None if failure == "legacy" else "$argon2id$broken"
            db.commit()
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert _unauthorized(response) == baseline
    assert dummy_calls == ([] if failure == "wrong-password" else [password])
    assert password not in response.text + caplog.text
    with factory() as db:
        assert db.scalars(select(SessionModel)).all() == []


def test_username_is_case_sensitive_and_password_is_not_trimmed(auth_api: AuthAPI) -> None:
    client, _, _ = auth_api
    response = client.post(
        "/api/auth/register", json={"username": "Alice", "password": "other-password"}
    )
    assert response.status_code == 201
    assert response.json()["user_id"] != _USER_ID
    _unauthorized(client.post("/api/auth/login", json={"username": "Alice", "password": _PASSWORD}))
    _unauthorized(
        client.post("/api/auth/login", json={"username": "alice", "password": _PASSWORD.strip()})
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "Alice", "password": "other-password"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("method,path", [("get", "/api/auth/me"), ("post", "/api/auth/logout")])
@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic YWxpY2U6cGFzc3dvcmQ=",
        "Bearer",
        "Bearer unknown",
        "Bearer " + "a" * 43,
        "Bearer token extra",
    ],
)
def test_missing_malformed_or_unknown_credentials_are_401(
    auth_api: AuthAPI, method: str, path: str, authorization: str | None
) -> None:
    client, _, _ = auth_api
    headers = {} if authorization is None else {"Authorization": authorization}
    _unauthorized(client.request(method, path, headers=headers))


def test_query_and_cookie_are_not_authentication_transports(auth_api: AuthAPI) -> None:
    client, factory, _ = auth_api
    token = _token(factory)
    client.cookies.set("access_token", token)
    client.cookies.set("session", token)
    _unauthorized(client.get("/api/auth/me", params={"access_token": token}))
    assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200


@pytest.mark.parametrize("state", ["expired", "revoked", "inactive", "deleted"])
def test_me_rechecks_session_and_user_state_for_each_request(
    auth_api: AuthAPI, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, _ = auth_api
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    monkeypatch.setattr(storage, "_utc_now", lambda: now)
    token = _token(factory)
    assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
    if state == "expired":
        monkeypatch.setattr(storage, "_utc_now", lambda: now + timedelta(hours=23))
        assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
        with factory() as db:
            saved = db.get(SessionModel, hashlib.sha256(token.encode()).hexdigest())
            assert saved.expires_at == now + timedelta(days=1)
        monkeypatch.setattr(storage, "_utc_now", lambda: now + timedelta(days=1))
    else:
        with factory() as db:
            if state == "revoked":
                storage.revoke_session(db, token)
            elif state == "inactive":
                db.get(UserModel, _USER_ID).is_active = False
                db.commit()
            else:
                db.delete(db.get(UserModel, _USER_ID))
                db.commit()
    _unauthorized(client.get("/api/auth/me", headers=_bearer(token)))
    _unauthorized(client.post("/api/auth/logout", headers=_bearer(token)))


def test_logout_revokes_only_current_session_and_has_empty_204(auth_api: AuthAPI) -> None:
    client, factory, _ = auth_api
    first, second = _token(factory), _token(factory)
    assert first != second
    response = client.post("/api/auth/logout", headers=_bearer(first))
    assert response.status_code == 204, response.text
    assert response.content == b""
    _no_store(response)
    with factory() as db:
        first_record = db.get(SessionModel, hashlib.sha256(first.encode()).hexdigest())
        second_record = db.get(SessionModel, hashlib.sha256(second.encode()).hexdigest())
        assert first_record.revoked_at is not None
        assert second_record.revoked_at is None
    _unauthorized(client.get("/api/auth/me", headers=_bearer(first)))
    _unauthorized(client.post("/api/auth/logout", headers=_bearer(first)))
    assert client.get("/api/auth/me", headers=_bearer(second)).status_code == 200


def test_cp1_local_user_is_not_an_auth_fallback(auth_api: AuthAPI) -> None:
    client, factory, app = auth_api
    app.state.settings.cp1_local_mode = True
    with factory() as db:
        db.add(UserModel(id=app.state.settings.cp1_local_user_id, username="cp1-local"))
        db.commit()
    _unauthorized(client.get("/api/auth/me"))
    _unauthorized(
        client.post("/api/auth/login", json={"username": "cp1-local", "password": _PASSWORD})
    )


def test_public_owner_dependency_uses_verified_session(auth_api: AuthAPI) -> None:
    client, factory, app = auth_api

    @app.get("/owner-probe")
    def owner_probe(owner: Annotated[str, Depends(current_owner_id)]) -> dict[str, str]:
        return {"owner_id": owner}

    token = _token(factory)
    response = client.get("/owner-probe", headers=_bearer(token), params={"user_id": "other"})
    assert response.status_code == 200, response.text
    assert response.json() == {"owner_id": _USER_ID}
    rejected = client.get("/owner-probe")
    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"


def test_openapi_documents_bearer_without_secrets_in_user_schema(auth_api: AuthAPI) -> None:
    client, _, _ = auth_api
    schema = client.get("/api/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    bearer_names = {
        name
        for name, definition in schemes.items()
        if definition.get("type") == "http" and definition.get("scheme") == "bearer"
    }
    assert bearer_names
    for path, method in [("/api/auth/me", "get"), ("/api/auth/logout", "post")]:
        operation = schema["paths"][path][method]
        assert any(set(requirement) & bearer_names for requirement in operation["security"])
        assert "401" in operation["responses"]
    assert set(schema["components"]["schemas"]["UserRead"]["properties"]) == _USER_KEYS
    register = schema["paths"]["/api/auth/register"]["post"]
    assert "201" in register["responses"]
    assert "409" in register["responses"]
    assert "204" in schema["paths"]["/api/auth/logout"]["post"]["responses"]


def test_invalid_auth_attempts_are_rate_limited_per_endpoint_and_window(
    auth_api: AuthAPI,
) -> None:
    client, _, app = auth_api
    now = 1_000.0
    app.state.auth_limiter._clock = lambda: now
    for path in ("/api/auth/register", "/api/auth/login"):
        for _ in range(10):
            _error(client.post(path, json={}), 422, "invalid_request")
        limited = client.post(path, json={})
        _error(limited, 429, "rate_limit")
        assert limited.headers["retry-after"] == "300"
        # Client-controlled forwarding headers must not choose a fresh IP bucket.
        spoofed = client.post(path, json={}, headers={"X-Forwarded-For": "198.51.100.2"})
        _error(spoofed, 429, "rate_limit")
    now += 299
    limited = client.post("/api/auth/login", json={})
    _error(limited, 429, "rate_limit")
    assert limited.headers["retry-after"] == "1"
    now += 1
    _error(client.post("/api/auth/login", json={}), 422, "invalid_request")


def test_malformed_json_does_not_reflect_password_or_dsn(auth_api: AuthAPI) -> None:
    client, _, app = auth_api
    response = client.post(
        "/api/auth/login",
        content='{"password":"private-malformed-secret","dsn":"postgresql://secret"',
        headers={"Content-Type": "application/json"},
    )
    _error(response, 422, "invalid_request")
    assert "private-malformed-secret" not in response.text
    assert "postgresql://secret" not in response.text
    _all_auth_slots_available(app.state.auth_limiter)


@pytest.mark.parametrize("transport", ["content-length", "chunked", "forged-length"])
def test_auth_body_limit_checks_received_bytes_before_hashing(
    auth_api: AuthAPI, transport: str
) -> None:
    client, factory, _ = auth_api
    secret = "private-oversized-secret"
    body = ('{"username":"alice","password":"' + secret * 1_000 + '"}').encode()
    headers = {"Content-Type": "application/json"}
    if transport == "forged-length":
        headers["Content-Length"] = "1"
    content = iter((body[:10_000], body[10_000:])) if transport == "chunked" else body
    response = client.post("/api/auth/login", content=content, headers=headers)
    assert response.status_code == 413, response.text
    assert ErrorResponse.model_validate(response.json()).error.message
    _no_store(response)
    assert secret not in response.text
    with factory() as db:
        assert db.scalars(select(SessionModel)).all() == []


def test_all_auth_error_responses_are_uncacheable(auth_api: AuthAPI) -> None:
    client, _, app = auth_api

    @app.get("/api/auth/failure-probe", include_in_schema=False)
    def failure_probe() -> None:
        raise RuntimeError("private-internal-password-and-dsn")

    _error(client.get("/api/auth/absent"), 404, "not_found")
    _error(client.get("/api/auth/login"), 405, "http_405")
    failure = client.get("/api/auth/failure-probe")
    _error(failure, 500, "internal_error")
    assert "private-internal-password-and-dsn" not in failure.text


@pytest.mark.parametrize(
    "endpoint,field,username",
    [
        ("register", "username", "new-user"),
        ("register", "password", "new-user"),
        ("register", "email", "new-user"),
        ("login", "username", "alice"),
        ("login", "password", "alice"),
        ("login", "password", "unknown-user"),
    ],
)
def test_lone_surrogate_is_sanitized_422_before_password_work(
    auth_api: AuthAPI, endpoint: str, field: str, username: str
) -> None:
    client, factory, _ = auth_api
    payload = {"username": username, "password": _PASSWORD}
    payload[field] = "private-invalid-\ud800"
    response = client.post(
        f"/api/auth/{endpoint}",
        content=json.dumps(payload, ensure_ascii=True),
        headers={"Content-Type": "application/json"},
    )
    _error(response, 422, "invalid_request")
    assert "private-invalid" not in response.text
    with factory() as db:
        assert len(db.scalars(select(UserModel)).all()) == 1
        assert db.scalars(select(SessionModel)).all() == []


def test_bearer_scheme_is_case_insensitive_but_duplicate_headers_are_rejected(
    auth_api: AuthAPI,
) -> None:
    client, factory, _ = auth_api
    token = _token(factory)
    assert (
        client.get("/api/auth/me", headers={"authorization": f"bEaReR {token}"}).status_code == 200
    )
    duplicate = client.get(
        "/api/auth/me",
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )
    _unauthorized(duplicate)


def test_slow_auth_body_times_out_without_calling_app_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_policy, "AUTH_BODY_TIMEOUT_SECONDS", 0)
    limiter = auth_policy.AuthRateLimiter(10, 300)
    messages: list[Message] = []

    async def unexpected_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        pytest.fail("An incomplete auth body must not reach parsing or password hashing")

    async def slow_receive() -> Message:
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"private-incomplete-password"}

    async def capture(message: Message) -> None:
        messages.append(message)

    middleware = auth_policy.AuthPolicyMiddleware(unexpected_app, prefix="/api", limiter=limiter)
    scope = {"type": "http", "method": "POST", "path": "/api/auth/login"}
    asyncio.run(middleware(scope, slow_receive, capture))
    start, body = messages
    assert start["status"] == 408
    headers = dict(start["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"pragma"] == b"no-cache"
    payload = ErrorResponse.model_validate_json(body["body"])
    assert payload.error.code == "request_timeout"
    assert b"private-incomplete-password" not in body["body"]
    _all_auth_slots_available(limiter)


def test_client_disconnect_before_auth_body_completion_releases_slot() -> None:
    limiter = auth_policy.AuthRateLimiter(10, 300)

    async def unexpected_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        pytest.fail("A disconnected auth request must not reach the application")

    async def disconnected() -> Message:
        return {"type": "http.disconnect"}

    async def unexpected_response(_message: Message) -> None:
        pytest.fail("Do not send a response after the client disconnected")

    middleware = auth_policy.AuthPolicyMiddleware(unexpected_app, prefix="/api", limiter=limiter)
    scope = {"type": "http", "method": "POST", "path": "/api/auth/register"}
    asyncio.run(middleware(scope, disconnected, unexpected_response))
    _all_auth_slots_available(limiter)


def test_four_busy_auth_slots_reject_further_work_and_allow_retry(auth_api: AuthAPI) -> None:
    client, _, app = auth_api
    limiter = app.state.auth_limiter
    for _ in range(4):
        assert limiter._slots.acquire(blocking=False)
    try:
        response = client.post("/api/auth/login", json={})
        _error(response, 429, "rate_limit")
        assert response.headers["retry-after"] == "1"
    finally:
        for _ in range(4):
            limiter._slots.release()
    _error(client.post("/api/auth/login", json={}), 422, "invalid_request")
    _all_auth_slots_available(limiter)


def test_auth_rate_bucket_capacity_is_bounded_and_expiration_is_not_extended() -> None:
    now = 0.0
    limiter = auth_policy.AuthRateLimiter(1, 300, clock=lambda: now)
    for index in range(4_096):
        assert limiter.check(f"client-{index}", "/api/auth/login") is None
    assert len(limiter._buckets) == 4_096
    assert limiter.check("new-client", "/api/auth/login") == 300
    now = 299.0
    assert limiter.check("new-client", "/api/auth/login") == 1
    assert limiter.check("client-0", "/api/auth/login") == 1
    assert len(limiter._buckets) == 4_096
    now = 300.0
    assert limiter.check("new-client", "/api/auth/login") is None
    assert len(limiter._buckets) == 1
    assert limiter.check("client-0", "/api/auth/login") is None


@pytest.mark.parametrize(
    "method,path,operation",
    [
        ("post", "/api/auth/register", "create_user"),
        ("post", "/api/auth/login", "authenticate_user"),
        ("post", "/api/auth/login", "create_session"),
        ("get", "/api/auth/me", "get_session_user"),
        ("post", "/api/auth/logout", "revoke_session"),
    ],
)
def test_storage_errors_are_sanitized_503_and_do_not_change_auth_state(
    auth_api: AuthAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    operation: str,
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    token = _token(factory) if path.endswith(("/me", "/logout")) else "private-token-param"
    secret = "postgresql://private-database-password@db/auth"
    failure = StatementError(
        secret,
        "UPDATE users SET password_hash=:password",
        {"password": _PASSWORD, "access_token": token},
        RuntimeError("private-storage-exception"),
    )

    def fail_storage(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    with factory() as db:
        before = (
            db.execute(UserModel.__table__.select()).all(),
            db.execute(SessionModel.__table__.select()).all(),
        )
    target = auth_dependencies if operation == "get_session_user" else auth_routes
    with monkeypatch.context() as patch:
        patch.setattr(target, operation, fail_storage)
        kwargs: dict[str, Any] = {"headers": _bearer(token)}
        if path.endswith(("/register", "/login")):
            kwargs["json"] = {
                "username": "new-user" if operation == "create_user" else "alice",
                "password": _PASSWORD,
            }
        response = client.request(method, path, **kwargs)
    _error(response, 503, "unavailable")
    for sensitive in (secret, _PASSWORD, token, "private-storage-exception", "UPDATE users"):
        assert sensitive not in response.text + caplog.text
    assert "set-cookie" not in response.headers
    with factory() as db:
        after = (
            db.execute(UserModel.__table__.select()).all(),
            db.execute(SessionModel.__table__.select()).all(),
        )
    assert after == before
    if path.endswith(("/me", "/logout")):
        assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200


def test_account_state_race_during_session_creation_is_generic_401(
    auth_api: AuthAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    baseline = _unauthorized(
        client.post("/api/auth/login", json={"username": "unknown", "password": _PASSWORD})
    )

    def fail_session(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("private-account-state-error")

    monkeypatch.setattr(auth_routes, "create_session", fail_session)
    response = client.post("/api/auth/login", json={"username": "alice", "password": _PASSWORD})
    assert _unauthorized(response) == baseline
    assert "private-account-state-error" not in response.text + caplog.text
    assert _PASSWORD not in response.text + caplog.text
    with factory() as db:
        assert len(db.scalars(select(UserModel)).all()) == 1
        assert db.scalars(select(SessionModel)).all() == []


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_registration_integrity_error_is_503_without_confirmed_username_conflict(
    auth_api: AuthAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lookup_fails: bool,
) -> None:
    client, factory, _ = auth_api
    caplog.set_level(logging.DEBUG)
    username = "alice" if lookup_fails else "new-user"
    secret = "private-integrity-secret"

    def fail_registration(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError(
            "INSERT INTO users",
            {"username": username, "password": _PASSWORD},
            RuntimeError(secret),
        )

    def fail_lookup(*_args: Any, **_kwargs: Any) -> None:
        raise StatementError(secret, "SELECT users", {"password": _PASSWORD}, RuntimeError(secret))

    with monkeypatch.context() as patch:
        patch.setattr(auth_routes, "create_user", fail_registration)
        if lookup_fails:
            patch.setattr(Session, "scalar", fail_lookup)
        response = client.post(
            "/api/auth/register", json={"username": username, "password": _PASSWORD}
        )
    _error(response, 503, "unavailable")
    for sensitive in (secret, _PASSWORD, "INSERT INTO users", "SELECT users"):
        assert sensitive not in response.text + caplog.text
    with factory() as db:
        assert len(db.scalars(select(UserModel)).all()) == 1
        assert db.scalars(select(SessionModel)).all() == []
