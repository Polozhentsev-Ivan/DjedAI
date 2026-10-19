"""Конфигурация приложения DjedAI (§7.4).

Загружает параметры из переменных окружения и .env-файла с безопасными дефолтами
для локальной разработки.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки сервиса Backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Общие параметры приложения
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_prefix: str = "/api"
    app_version: str = "0.1.0"

    # БД приложения (метаданные, пользователи, чаты, дашборды)
    app_database_url: str = Field(default="sqlite:///./var/app.db", alias="APP_DATABASE_URL")

    # Параметры инференса модели (vLLM / локальный endpoint)
    model_base_url: str = Field(default="http://localhost:8000/v1", alias="MODEL_BASE_URL")
    model_name: str = Field(default="Qwen/Qwen2.5-Coder-32B-Instruct", alias="MODEL_NAME")
    model_revision: str | None = Field(default=None, alias="MODEL_REVISION")

    # Лимиты безопасности
    sql_max_rows: int = Field(default=5000, alias="SQL_MAX_ROWS")
    tool_timeout_seconds: float = Field(default=30.0, alias="TOOL_TIMEOUT_SECONDS")

    # Сетевая политика (исходящий трафик только по whitelist)
    allowed_outbound_hosts: str = Field(default="", alias="ALLOWED_OUTBOUND_HOSTS")

    # Секрет сессии / JWT
    session_secret: str = Field(
        default="dev-session-secret-change-in-production", alias="SESSION_SECRET"
    )

    # Разрешенные CORS origins
    cors_origins: list[str] = ["*"]

    @field_validator(
        "app_env",
        "log_level",
        "app_database_url",
        "model_base_url",
        "model_name",
        "model_revision",
        "sql_max_rows",
        "tool_timeout_seconds",
        "allowed_outbound_hosts",
        "session_secret",
        mode="before",
    )
    @classmethod
    def empty_str_to_default(cls, v: Any, info: ValidationInfo) -> Any:
        """Подставляет значение по умолчанию, если параметр передан пустой строкой."""
        if isinstance(v, str) and v.strip() == "":
            field = cls.model_fields.get(info.field_name)
            if field is not None:
                return field.get_default(call_default_factory=True)
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()
