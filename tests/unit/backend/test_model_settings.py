"""Настройка модели из интерфейса поверх .env (#209)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from djedai.agent.config import data_sharing_allowed
from djedai.agent.data_policy import DataCategory, DataContext
from djedai.backend.app import create_app
from djedai.backend.auth import create_session, hash_password
from djedai.backend.chat_service import get_agent_runtime
from djedai.backend.config import Settings
from djedai.backend.db.models import ModelSettingsModel
from djedai.backend.db.repositories import get_or_create_user
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.model_settings import ModelSettingsService, model_settings_for
from djedai.config import get_limits
from djedai.contracts.settings import ModelSettingsUpdate

ENV_URL = "https://env.example/v1"
ENV_MODEL = "env-model"
ENV_KEY = "sk-env-key-0000"
UI_KEY = "sk-ui-key-marker-7f3a"


@pytest.fixture(autouse=True)
def model_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The developer's .env must not leak into these checks.
    monkeypatch.setattr("djedai.agent.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr("djedai.agent.data_policy.dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setenv("MODEL_BASE_URL", ENV_URL)
    monkeypatch.setenv("MODEL_NAME", ENV_MODEL)
    monkeypatch.setenv("MODEL_API_KEY", ENV_KEY)
    monkeypatch.setenv("ALLOW_DATA_SHARING", "true")
    monkeypatch.setenv("MODEL_REASONING_PLAN", "high")
    monkeypatch.setenv("MODEL_REASONING_SUPPORTED", "true")
    get_limits.cache_clear()
    yield
    get_limits.cache_clear()


AppFactory = Callable[..., FastAPI]
PASSWORD_HASH = hash_password("model-settings-password")


@pytest.fixture
def make_app(tmp_path: Path) -> Iterator[AppFactory]:
    """Приложение на общей SQLite-базе; повторный вызов имитирует перезапуск."""
    engines = []
    key = Fernet.generate_key().decode()

    def make(**overrides: Any) -> FastAPI:
        engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
        engines.append(engine)
        init_db(engine)
        values: dict[str, Any] = {
            "_env_file": None,
            "model_allowed_names": "env-model,other-model",
            "source_encryption_key": key,
            "model_input_price_usd": "0.1",
            "model_output_price_usd": "0.2",
            "model_price_reference": "test tariff",
            "diagnostics_dir": str(tmp_path / "diagnostics"),
            "results_data_dir": str(tmp_path / "results"),
            **overrides,
        }
        app = create_app(Settings(**values))
        app.state.session_factory = sessionmaker(bind=engine)
        with app.state.session_factory() as db:
            user = get_or_create_user(db, "u_admin", "admin")
            user.password_hash = PASSWORD_HASH
            db.commit()
        return app

    yield make
    for engine in engines:
        engine.dispose()


def _runtime(app: FastAPI):
    app.state.dataset_service = SimpleNamespace(get_context=None)
    return get_agent_runtime(Request({"type": "http", "app": app}))


def _update(app: FastAPI, **values: Any):
    return model_settings_for(app).update(ModelSettingsUpdate(**values), "u_admin")


def test_env_only_keeps_previous_behaviour(make_app: AppFactory) -> None:
    app = make_app()
    view = model_settings_for(app).view()
    assert (view.base_url, view.model_name) == (ENV_URL, ENV_MODEL)
    assert view.api_key_set and not view.api_key_from_ui and view.matches_env
    assert view.editable_connection is False
    runtime = _runtime(app)
    assert runtime.client.model == ENV_MODEL
    assert runtime.client.reasoning_for("plan") == "high"
    assert runtime.tariff is not None and runtime.tariff.input_usd == Decimal("0.1")


def _assign(app: FastAPI, user_id: str, key: str | None) -> None:
    from djedai.backend.user_model_keys import UserModelKeys

    service = model_settings_for(app)
    UserModelKeys(app.state.session_factory, service.secrets).assign(user_id, key)


def test_operator_keys_are_isolated_encrypted_and_fall_back_only_when_removed(
    make_app: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    from djedai.backend.db.models import UserModelKeyModel
    from djedai.backend.source_secrets import SourceSecretError

    app = make_app()
    with app.state.session_factory() as db:
        get_or_create_user(db, "u_second", "second")
        db.commit()
    _assign(app, "u_admin", UI_KEY)
    _assign(app, "u_second", "second-key-marker")
    with app.state.session_factory() as db:
        first = db.get(UserModelKeyModel, "u_admin")
        second = db.get(UserModelKeyModel, "u_second")
        assert UI_KEY not in first.api_key_ciphertext
        # Copying another user's ciphertext must fail even under the same server key.
        first.api_key_ciphertext = second.api_key_ciphertext
        db.commit()
    with pytest.raises(SourceSecretError):
        model_settings_for(app, "u_admin").override()
    app.state.dataset_service = SimpleNamespace(get_context=None)
    assert get_agent_runtime(Request({"type": "http", "app": app}), "u_admin").unavailable
    _assign(app, "u_admin", UI_KEY)
    for user, expected in [("u_admin", UI_KEY), ("u_second", "second-key-marker")]:
        runtime = get_agent_runtime(Request({"type": "http", "app": app}), user)
        assert runtime.client.client.api_key == expected
        runtime.client.client.close()
    _assign(app, "u_admin", None)
    runtime = get_agent_runtime(Request({"type": "http", "app": app}), "u_admin")
    assert runtime.client.client.api_key == ENV_KEY
    runtime.client.client.close()
    assert UI_KEY not in caplog.text and "second-key-marker" not in caplog.text


def test_assigned_key_never_uses_a_global_user_supplied_endpoint(make_app: AppFactory) -> None:
    app = make_app(model_settings_from_ui=True)
    _update(app, base_url="https://other.example/v1", api_key="global-ui-key")
    _assign(app, "u_admin", UI_KEY)
    service = model_settings_for(app, "u_admin")
    assert service.override().base_url == ENV_URL
    assert service.override().api_key == UI_KEY
    assert service.view().editable_connection is False
    with pytest.raises(HTTPException) as rejected:
        service.update(ModelSettingsUpdate(base_url="https://third.example/v1"), "u_admin")
    assert rejected.value.status_code == 403


def test_http_model_checks_use_authenticated_users_assigned_keys(
    api: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = api()
    with client.app.state.session_factory() as db:
        second_user = get_or_create_user(db, "u_second", "second")
        second_user.password_hash = PASSWORD_HASH
        db.commit()
        second_token = create_session(db, "u_second")
    _assign(client.app, "u_admin", UI_KEY)
    _assign(client.app, "u_second", "second-key-marker")
    seen = []

    def build(values):
        seen.append(values)
        return _FailingClient(None)

    monkeypatch.setattr("djedai.backend.model_settings.get_agent_client", build)
    first = client.post("/api/settings/model/check", json={})
    second = client.post(
        "/api/settings/model/check", json={}, headers={"Authorization": f"Bearer {second_token}"}
    )
    assert first.json()["ok"] and second.json()["ok"]
    assert [value.api_key for value in seen] == [UI_KEY, "second-key-marker"]
    for url in ("/api/settings/model", "/api/auth/me"):
        assert UI_KEY not in client.get(url).text
    assert client.put("/api/settings/model", json={"api_key": "replace"}).status_code == 403
    assert client.put("/api/settings/model", json={"user_id": "u_second"}).status_code == 422


def test_catalog_description_job_uses_its_owner_key(
    api: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    used = []

    def describe(database, catalog, model, **kwargs):
        used.append(model.client.api_key)
        model.client.close()
        return catalog

    monkeypatch.setattr("djedai.backend.dataset_service.describe_dataset", describe)
    client = api(catalog_descriptions=True, datasets_data_dir=str(tmp_path / "catalogs"))
    _assign(client.app, "u_admin", UI_KEY)
    upload = client.post(
        "/api/datasets/upload", files={"files": ("data.csv", b"city,count\nA,5\nB,7\n", "text/csv")}
    )
    assert upload.status_code == 202
    events = client.get(f"/api/datasets/{upload.json()['dataset_id']}/events")
    assert "event: done" in events.text and used == [UI_KEY]


def test_ui_values_apply_to_the_next_turn_without_restart(make_app: AppFactory) -> None:
    app = make_app(model_settings_from_ui=True)
    assert _runtime(app).client.model == ENV_MODEL
    _update(app, base_url="http://localhost:8000/v1", model_name="local-model", api_key=UI_KEY)
    client = _runtime(app).client
    assert client.model == "local-model"
    assert str(client.client.base_url) == "http://localhost:8000/v1/"
    assert client.client.api_key == UI_KEY


def test_chat_works_without_env_model_values(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
        monkeypatch.delenv(name)
    app = make_app(model_settings_from_ui=True)
    assert _runtime(app).unavailable
    _update(app, base_url="http://localhost:8000/v1", model_name="local-model", api_key=UI_KEY)
    runtime = _runtime(app)
    assert not runtime.unavailable
    assert runtime.client.model == "local-model"


def test_saved_settings_survive_restart(make_app: AppFactory) -> None:
    _update(make_app(model_settings_from_ui=True), model_name="other-model", api_key=UI_KEY)
    restarted = make_app(model_settings_from_ui=True)
    view = model_settings_for(restarted).view()
    assert view.model_name == "other-model" and view.api_key_from_ui
    assert _runtime(restarted).client.client.api_key == UI_KEY


@pytest.mark.parametrize(
    "change", [{"model_name": "other-model"}, {"base_url": "http://localhost:8000/v1"}]
)
def test_new_pair_resets_unconfirmed_flags(make_app: AppFactory, change: dict[str, str]) -> None:
    app = make_app(model_settings_from_ui=True)
    service = model_settings_for(app)
    assert data_sharing_allowed(service.override()) is True
    view = _update(app, **change)
    assert view.matches_env is False
    runtime = _runtime(app)
    assert runtime.client.reasoning_for("plan") == "provider_default"
    assert runtime.tariff is None
    assert data_sharing_allowed(service.override()) is False
    assert not runtime.client.data_policy.allows(DataCategory.CELL_VALUE, DataContext.CATALOG)
    # Returning to the .env pair restores what the administrator confirmed for it.
    _update(app, model_name="", base_url="")
    runtime = _runtime(app)
    assert runtime.client.reasoning_for("plan") == "high"
    assert runtime.tariff is not None
    assert data_sharing_allowed(service.override()) is True
    assert runtime.client.data_policy.allows(DataCategory.CELL_VALUE, DataContext.CATALOG)


def test_only_a_new_key_keeps_the_confirmed_pair(make_app: AppFactory) -> None:
    app = make_app(model_settings_from_ui=True)
    assert _update(app, api_key=UI_KEY).matches_env
    runtime = _runtime(app)
    assert runtime.client.reasoning_for("plan") == "high" and runtime.tariff is not None


def test_flag_off_ignores_saved_key_and_url(make_app: AppFactory) -> None:
    _update(
        make_app(model_settings_from_ui=True),
        base_url="http://attacker.example/v1",
        model_name="other-model",
        api_key=UI_KEY,
    )
    server = make_app(model_settings_from_ui=False)
    client = _runtime(server).client
    assert str(client.client.base_url) == ENV_URL + "/"
    assert client.client.api_key == ENV_KEY
    assert client.model == "other-model"
    view = model_settings_for(server).view()
    assert view.base_url == ENV_URL and not view.api_key_from_ui


def test_model_removed_from_allowed_list_falls_back_to_env(make_app: AppFactory) -> None:
    _update(make_app(), model_name="other-model")
    narrowed = make_app(model_allowed_names="")
    assert _runtime(narrowed).client.model == ENV_MODEL


def test_key_is_stored_only_encrypted(make_app: AppFactory) -> None:
    app = make_app(model_settings_from_ui=True)
    _update(app, api_key=UI_KEY)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as db:
        row = db.scalars(select(ModelSettingsModel)).one()
        assert row.api_key_ciphertext and UI_KEY not in row.api_key_ciphertext
        assert row.updated_by == "u_admin"
    assert UI_KEY not in repr(model_settings_for(app).override())


def test_key_unreadable_under_another_server_key_fails_closed(make_app: AppFactory) -> None:
    _update(make_app(model_settings_from_ui=True), api_key=UI_KEY)
    rotated = make_app(model_settings_from_ui=True, source_encryption_key=Fernet.generate_key())
    # The .env key is not sent instead of the saved one.
    assert _runtime(rotated).unavailable


# --- HTTP API -----------------------------------------------------------------


@pytest.fixture
def api(make_app: AppFactory) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def open_client(**overrides: Any) -> TestClient:
        app = make_app(**overrides)
        with app.state.session_factory() as db:
            token = create_session(db, "u_admin")
        client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        client.__enter__()
        clients.append(client)
        return client

    yield open_client
    for client in clients:
        client.__exit__(None, None, None)


def test_settings_require_a_session(api: Callable[..., TestClient]) -> None:
    client = api()
    anonymous = TestClient(client.app)
    assert anonymous.get("/api/settings/model").status_code == 401
    assert anonymous.put("/api/settings/model", json={"model_name": "x"}).status_code == 401
    assert client.get("/api/settings/model").headers["cache-control"] == "no-store"


def test_server_mode_hides_connection_and_limits_models(api: Callable[..., TestClient]) -> None:
    client = api(model_settings_from_ui=False)
    body = client.get("/api/settings/model").json()
    assert body["editable_connection"] is False
    assert body["allowed_models"] == ["env-model", "other-model"]
    for change in ({"api_key": UI_KEY}, {"base_url": "http://x/v1"}, {"api_key": ""}):
        response = client.put("/api/settings/model", json={"model_name": "other-model", **change})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        check = client.post("/api/settings/model/check", json=change)
        assert check.status_code == 403
    # Nothing was saved by the rejected requests.
    assert client.get("/api/settings/model").json()["model_name"] == ENV_MODEL

    outside = client.put("/api/settings/model", json={"model_name": "arbitrary/model"})
    assert outside.status_code == 422
    assert "arbitrary" not in outside.text
    chosen = client.put("/api/settings/model", json={"model_name": "other-model"})
    assert chosen.status_code == 200
    assert chosen.json()["model_name"] == "other-model"
    assert chosen.json()["matches_env"] is False


def test_local_mode_sets_key_url_and_any_model(api: Callable[..., TestClient]) -> None:
    client = api(model_settings_from_ui=True)
    assert client.get("/api/settings/model").json()["editable_connection"] is True
    response = client.put(
        "/api/settings/model",
        json={"base_url": "http://localhost:8000/v1", "model_name": "my/local", "api_key": UI_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] and body["api_key_from_ui"]
    assert (body["base_url"], body["model_name"]) == ("http://localhost:8000/v1", "my/local")
    assert UI_KEY not in response.text
    assert UI_KEY not in client.get("/api/settings/model").text
    # An empty key returns to .env.
    cleared = client.put("/api/settings/model", json={"api_key": ""}).json()
    assert cleared["api_key_set"] and not cleared["api_key_from_ui"]


@pytest.mark.parametrize(
    "payload", [{"base_url": "ftp://host"}, {"base_url": "localhost"}, {"api_key": "a\nb"}]
)
def test_local_mode_rejects_invalid_values(
    api: Callable[..., TestClient], payload: dict[str, str]
) -> None:
    response = api(model_settings_from_ui=True).put("/api/settings/model", json=payload)
    assert response.status_code == 422


def test_saving_a_key_requires_server_encryption_key(api: Callable[..., TestClient]) -> None:
    client = api(model_settings_from_ui=True, source_encryption_key=None)
    response = client.put("/api/settings/model", json={"api_key": UI_KEY})
    assert response.status_code == 409
    assert "SOURCE_ENCRYPTION_KEY" in response.json()["error"]["message"]
    assert UI_KEY not in response.text


def test_unknown_fields_are_rejected_without_echo(api: Callable[..., TestClient]) -> None:
    response = api(model_settings_from_ui=True).put(
        "/api/settings/model", json={"api_key": UI_KEY, "reasoning_supported": True}
    )
    assert response.status_code == 422
    assert UI_KEY not in response.text


class _FailingClient:
    def __init__(self, error: Exception | None) -> None:
        self.model = "m"
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.client = self

    def with_options(self, **options: Any) -> _FailingClient:
        assert options["max_retries"] == 0 and options["timeout"] <= 10
        return self

    @property
    def chat(self) -> _FailingClient:
        return self

    @property
    def completions(self) -> _FailingClient:
        return self

    def create(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://model.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls(f"rejected {UI_KEY}", response=response, body={"error": f"model {UI_KEY}"})


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (None, "ok"),
        (_status_error(openai.AuthenticationError, 401), "invalid_key"),
        (_status_error(openai.PermissionDeniedError, 403), "invalid_key"),
        (_status_error(openai.NotFoundError, 404), "unknown_model"),
        (_status_error(openai.BadRequestError, 400), "unknown_model"),
        (_status_error(openai.InternalServerError, 500), "model_error"),
        (openai.APIConnectionError(request=httpx.Request("POST", "http://x")), "unreachable"),
        (RuntimeError(UI_KEY), "model_error"),
    ],
)
def test_check_explains_failures_without_the_key(
    api: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception | None,
    code: str,
) -> None:
    client = api(model_settings_from_ui=True)
    fake = _FailingClient(error)
    seen: list[Any] = []

    def build(values: Any) -> _FailingClient:
        seen.append(values)
        return fake

    monkeypatch.setattr("djedai.backend.model_settings.get_agent_client", build)
    response = client.post("/api/settings/model/check", json={"api_key": UI_KEY})
    assert response.status_code == 200
    assert response.json()["code"] == code
    assert response.json()["ok"] is (code == "ok")
    assert seen[0].api_key == UI_KEY and fake.calls[0]["max_tokens"] == 1
    assert UI_KEY not in response.text and UI_KEY not in caplog.text
    # The check never saves what it was given.
    assert client.get("/api/settings/model").json()["api_key_from_ui"] is False


def test_check_reports_missing_configuration(
    api: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_API_KEY")
    response = api(model_settings_from_ui=True).post("/api/settings/model/check", json={})
    assert response.json()["code"] == "not_configured"


@pytest.mark.parametrize("status", [200, 400])
def test_check_has_a_total_deadline_and_closes_the_slow_response(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, model_endpoint: Callable, status: int
) -> None:
    from djedai.agent.client import BIModelClient

    with model_endpoint(
        lambda *_: {
            "mode": "drip",
            "status": status,
            "byte_delay_seconds": 0.01,
            "body": {"reply": "pong"},
            "error_body": {"error": {"message": "unknown model " + "x" * 200}},
        }
    ) as endpoint:
        model = BIModelClient("fixture-key", endpoint.base_url, "fixture", max_retries=3)
        monkeypatch.setattr("djedai.backend.model_settings.get_agent_client", lambda _: model)
        monkeypatch.setattr("djedai.backend.model_settings.CHECK_TIMEOUT_SECONDS", 0.25)
        service = model_settings_for(make_app())
        started = time.monotonic()
        try:
            result = service.check(ModelSettingsUpdate())
            elapsed = time.monotonic() - started
            assert not result.ok and result.code == "unreachable"
            assert elapsed < 1.25
            assert len(endpoint.calls) == 1
            assert endpoint.peer_closed.wait(1)
            assert model.client.is_closed()
        finally:
            model.client.close()


def test_check_preserves_fast_provider_error_classification(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, model_endpoint: Callable
) -> None:
    from djedai.agent.client import BIModelClient

    with model_endpoint(
        lambda *_: {
            "status": 400,
            "body": {},
            "error_body": {"error": {"message": "unknown model"}},
        }
    ) as endpoint:
        model = BIModelClient("fixture-key", endpoint.base_url, "fixture")
        monkeypatch.setattr("djedai.backend.model_settings.get_agent_client", lambda _: model)
        try:
            result = model_settings_for(make_app()).check(ModelSettingsUpdate())
            assert result.code == "unknown_model"
            assert len(endpoint.calls) == 1
            assert model.client.is_closed()
        finally:
            model.client.close()


def test_check_does_not_replace_unreadable_key_with_environment(
    api: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = api(model_settings_from_ui=True)
    assert first.put("/api/settings/model", json={"api_key": UI_KEY}).status_code == 200
    rotated = api(model_settings_from_ui=True, source_encryption_key=Fernet.generate_key())
    built = []
    monkeypatch.setattr(
        "djedai.backend.model_settings.get_agent_client", lambda value: built.append(value)
    )
    for change in ({}, {"model_name": "other-model"}):
        result = rotated.post("/api/settings/model/check", json=change)
        assert result.status_code == 200 and result.json()["ok"] is False
    assert built == []


@pytest.mark.parametrize("replacement", ["sk-explicit-replacement", ""])
def test_check_can_explicitly_replace_an_unreadable_key(
    api: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    first = api(model_settings_from_ui=True)
    saved_url = "https://saved.example/v1"
    first.put("/api/settings/model", json={"api_key": UI_KEY, "base_url": saved_url})
    rotated = api(model_settings_from_ui=True, source_encryption_key=Fernet.generate_key())
    seen = []

    def build(value):
        seen.append(value)
        return _FailingClient(None)

    monkeypatch.setattr("djedai.backend.model_settings.get_agent_client", build)
    result = rotated.post("/api/settings/model/check", json={"api_key": replacement})
    assert result.status_code == 200 and result.json()["ok"] is True
    assert seen[0].base_url == saved_url and seen[0].api_key == replacement
    runtime = get_agent_runtime(Request({"type": "http", "app": rotated.app}))
    assert runtime.unavailable  # A check never persists the replacement.


def test_description_client_and_sharing_use_one_snapshot(
    api: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    model_endpoint: Callable,
    tmp_path: Path,
) -> None:
    marker = "CATALOG_RACE_CELL_MARKER"

    def reply(_number, payload):
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "TableDescriptions":
            data = json.loads(payload["messages"][1]["content"])
            return {
                "body": {
                    "table": "Описание таблицы",
                    "columns": [
                        {"name": column["name"], "description": "Описание колонки"}
                        for column in data["columns"]
                    ],
                }
            }
        assert schema == "DatasetSummary"
        return {"body": {"summary": "Описание набора"}}

    with model_endpoint(reply) as allowed, model_endpoint(reply) as other:
        monkeypatch.setenv("MODEL_BASE_URL", allowed.base_url)
        client = api(
            model_settings_from_ui=True,
            catalog_descriptions=True,
            datasets_data_dir=str(tmp_path / "catalogs"),
        )
        original = ModelSettingsService.override
        reads = []

        def snapshot(service):
            captured = original(service)
            reads.append(captured)
            if len(reads) == 1:
                switched = client.put("/api/settings/model", json={"base_url": other.base_url})
                assert switched.status_code == 200
            return captured

        monkeypatch.setattr(ModelSettingsService, "override", snapshot)
        upload = client.post(
            "/api/datasets/upload",
            files={"files": ("accounts.csv", f"label\n{marker}\n".encode(), "text/csv")},
        )
        assert upload.status_code == 202
        events = client.get(f"/api/datasets/{upload.json()['dataset_id']}/events")
        assert "event: done" in events.text
        assert allowed.calls and not other.calls
        assert marker in json.dumps(allowed.calls)
        assert len(reads) == 1

        upload = client.post(
            "/api/datasets/upload",
            files={"files": ("second.csv", f"label\n{marker}\n".encode(), "text/csv")},
        )
        assert upload.status_code == 202
        client.get(f"/api/datasets/{upload.json()['dataset_id']}/events")
        assert other.calls and marker not in json.dumps(other.calls)


def test_ui_key_never_leaves_the_server(
    api: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Ключ не попадает в ответы, SSE, логи, диагностику и сохранённые результаты."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("MODEL_MAX_RETRIES", "0")
    get_limits.cache_clear()
    client = api(
        model_settings_from_ui=True,
        datasets_data_dir=str(tmp_path / "datasets"),
        catalog_descriptions=True,
    )
    texts = [
        client.put(
            "/api/settings/model",
            json={
                "base_url": "http://127.0.0.1:9/v1",
                "model_name": "leak/model",
                "api_key": UI_KEY,
            },
        ).text,
        client.get("/api/settings/model").text,
        client.post("/api/settings/model/check", json={}).text,
    ]
    upload = client.post(
        "/api/datasets/upload", files={"files": ("sales.csv", b"amount\n10\n20\n", "text/csv")}
    )
    assert upload.status_code == 202
    dataset_id = upload.json()["dataset_id"]
    texts.append(client.get(f"/api/datasets/{dataset_id}/events").text)
    texts.append(client.get(f"/api/datasets/{dataset_id}").text)
    chat_id = client.post("/api/chats", json={"dataset_id": dataset_id}).json()["chat_id"]
    stream = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сумма amount?"})
    assert "event: error" in stream.text  # The real client really tried the UI endpoint.
    texts += [stream.text, client.get(f"/api/chats/{chat_id}").text]

    for text in texts:
        assert UI_KEY not in text
    assert UI_KEY not in caplog.text
    marker = UI_KEY.encode()
    stored = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert any(path.name == "agent.jsonl" for path in stored)  # Diagnostics were written.
    for path in stored:
        assert marker not in path.read_bytes(), path.name
