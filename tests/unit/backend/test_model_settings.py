"""Настройка модели из интерфейса поверх .env (#209)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from djedai.agent.config import data_sharing_allowed
from djedai.backend.app import create_app
from djedai.backend.chat_service import get_agent_runtime
from djedai.backend.config import Settings
from djedai.backend.db.models import ModelSettingsModel
from djedai.backend.db.repositories import get_or_create_user
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.model_settings import model_settings_for
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
    monkeypatch.setattr("djedai.agent.config.dotenv_values", lambda *args, **kwargs: {})
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
            get_or_create_user(db, "u_admin", "admin")
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
    # Returning to the .env pair restores what the administrator confirmed for it.
    _update(app, model_name="", base_url="")
    runtime = _runtime(app)
    assert runtime.client.reasoning_for("plan") == "high"
    assert runtime.tariff is not None
    assert data_sharing_allowed(service.override()) is True


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
