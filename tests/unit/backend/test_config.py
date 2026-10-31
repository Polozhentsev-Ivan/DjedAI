"""Тесты модуля конфигурации backend/config.py."""

from __future__ import annotations

from djedai.backend.config import Settings, get_settings


def test_default_settings() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.api_prefix == "/api"
    assert settings.sql_max_rows == 5000
    assert settings.tool_timeout_seconds == 30.0
    assert not settings.is_production


def test_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SQL_MAX_ROWS", "1000")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://user:pass@localhost:5432/djedai")

    settings = Settings(_env_file=None)
    assert settings.app_env == "production"
    assert settings.log_level == "DEBUG"
    assert settings.sql_max_rows == 1000
    assert settings.app_database_url == "postgresql://user:pass@localhost:5432/djedai"
    assert settings.is_production


def test_get_settings_cached() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_settings_empty_env_values(monkeypatch) -> None:
    monkeypatch.setenv("APP_DATABASE_URL", "")
    monkeypatch.setenv("MODEL_BASE_URL", "")
    monkeypatch.setenv("MODEL_NAME", "")
    monkeypatch.setenv("MODEL_REVISION", "")
    monkeypatch.setenv("SQL_MAX_ROWS", "")
    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "")
    monkeypatch.setenv("ALLOWED_OUTBOUND_HOSTS", "")
    monkeypatch.setenv("SESSION_SECRET", "")

    settings = Settings(_env_file=None)
    assert settings.sql_max_rows == 5000
    assert settings.tool_timeout_seconds == 30.0
    assert settings.app_database_url == "sqlite:///./var/app.db"
    assert settings.model_base_url == "https://api.deepinfra.com/v1/openai"
    assert settings.model_name == "Qwen/Qwen3.6-27B"
    assert settings.model_revision is None
    assert settings.session_secret == "dev-session-secret-change-in-production"


def test_settings_reject_zero_row_limit(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("SQL_MAX_ROWS", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_settings_cors_origins_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_settings_cors_origins_env_override(monkeypatch) -> None:
    # Запятая как разделитель
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "http://localhost:3000, http://127.0.0.1:3000")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]

    # JSON-массив
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", '["http://localhost:5173"]')
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:5173"]

    # Пустая строка (blank enables nothing)
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == []


def test_settings_cors_origins_disallow_wildcard(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="wildcard.*запрещен"):
        Settings(_env_file=None)

    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "http://localhost:5173, *")
    with pytest.raises(ValueError, match="wildcard.*запрещен"):
        Settings(_env_file=None)


def test_settings_cors_origins_invalid_json(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "[not a valid json")
    with pytest.raises(ValueError, match="Некорректный JSON"):
        Settings(_env_file=None)


def test_model_settings_from_ui_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MODEL_SETTINGS_FROM_UI", raising=False)
    assert Settings(_env_file=None).model_settings_from_ui is False
    monkeypatch.setenv("MODEL_SETTINGS_FROM_UI", "")
    assert Settings(_env_file=None).model_settings_from_ui is False
    monkeypatch.setenv("MODEL_SETTINGS_FROM_UI", "true")
    assert Settings(_env_file=None).model_settings_from_ui is True


def test_model_allowed_names_always_include_model_name(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_NAME", "base-model")
    monkeypatch.delenv("MODEL_ALLOWED_NAMES", raising=False)
    assert Settings(_env_file=None).model_allowed_names == ["base-model"]

    monkeypatch.setenv("MODEL_ALLOWED_NAMES", " other , base-model,other,, ")
    assert Settings(_env_file=None).model_allowed_names == ["other", "base-model"]

    monkeypatch.setenv("MODEL_ALLOWED_NAMES", '["a", "b"]')
    assert Settings(_env_file=None).model_allowed_names == ["base-model", "a", "b"]


def test_model_allowed_names_reject_invalid_json(monkeypatch) -> None:
    import pytest
    from pydantic import ValidationError

    monkeypatch.setenv("MODEL_ALLOWED_NAMES", '["a"')
    with pytest.raises(ValidationError, match="MODEL_ALLOWED_NAMES"):
        Settings(_env_file=None)
