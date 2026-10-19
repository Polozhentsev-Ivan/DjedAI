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
    assert settings.model_base_url == "http://localhost:8000/v1"
    assert settings.model_name == "Qwen/Qwen2.5-Coder-32B-Instruct"
    assert settings.model_revision is None
    assert settings.session_secret == "dev-session-secret-change-in-production"


def test_settings_explicit_zero_preserved(monkeypatch) -> None:
    monkeypatch.setenv("SQL_MAX_ROWS", "0")
    settings = Settings(_env_file=None)
    assert settings.sql_max_rows == 0
