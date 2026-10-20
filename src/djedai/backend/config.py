"""Конфигурация приложения DjedAI (§7.4).

Загружает параметры из переменных окружения и .env-файла с безопасными дефолтами
для локальной разработки.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import SettingsConfigDict

from djedai.config import LimitSettings


class Settings(LimitSettings):
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
    app_version: str = "0.2.0"

    # БД приложения (метаданные, пользователи, чаты, дашборды)
    app_database_url: str = Field(default="sqlite:///./var/app.db", alias="APP_DATABASE_URL")
    results_data_dir: str = Field(default="./var/data/results", alias="RESULTS_DATA_DIR")

    # Параметры инференса модели (vLLM / локальный endpoint)
    model_base_url: str = Field(default="http://localhost:8000/v1", alias="MODEL_BASE_URL")
    model_name: str = Field(default="Qwen/Qwen2.5-Coder-32B-Instruct", alias="MODEL_NAME")
    model_revision: str | None = Field(default=None, alias="MODEL_REVISION")

    # Сетевая политика (исходящий трафик только по whitelist)
    allowed_outbound_hosts: str = Field(default="", alias="ALLOWED_OUTBOUND_HOSTS")

    # Секрет сессии / JWT
    session_secret: str = Field(
        default="dev-session-secret-change-in-production", alias="SESSION_SECRET"
    )

    # Разрешенные CORS origins (явный allowlist для dev-сервера frontend)
    cors_origins: list[str] | str = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        alias="BACKEND_CORS_ORIGINS",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def validate_cors_origins(cls, v: Any) -> list[str]:
        """Парсит и валидирует список разрешенных dev CORS origins.

        - None: использует дефолтный dev allowlist (http://localhost:5173, http://127.0.0.1:5173).
        - Пустая строка / пробелы: пустой список [] (blank enables nothing).
        - Строка через запятую или JSON-массив: парсится в список адресов.
        - Wildcard '*' запрещен согласно соглашению безопасности.
        """
        if v is None:
            return ["http://localhost:5173", "http://127.0.0.1:5173"]
        if isinstance(v, str):
            val = v.strip()
            if not val:
                return []
            if val.startswith("["):
                try:
                    import json

                    parsed = json.loads(val)
                except Exception as e:
                    raise ValueError(f"Некорректный JSON в BACKEND_CORS_ORIGINS: {e}") from e
                if not isinstance(parsed, list):
                    raise ValueError(
                        "BACKEND_CORS_ORIGINS в формате JSON должен быть массивом строк"
                    )
                origins = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                origins = [part.strip() for part in val.split(",") if part.strip()]
        elif isinstance(v, (list, tuple, set)):
            origins = [str(item).strip() for item in v if str(item).strip()]
        else:
            origins = [str(v).strip()]

        if "*" in origins:
            raise ValueError(
                "Использование wildcard '*' для CORS запрещено. "
                "Задайте явный список разрешенных dev origins (например, http://localhost:5173)."
            )
        return origins

    @field_validator(
        "app_env",
        "log_level",
        "app_database_url",
        "results_data_dir",
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
