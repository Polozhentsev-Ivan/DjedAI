"""Конфигурация приложения DjedAI (§7.4).

Загружает параметры из переменных окружения и .env-файла с безопасными дефолтами
для локальной разработки.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
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
    diagnostics_dir: str = Field(default="./var/diagnostics", alias="DIAGNOSTICS_DIR")
    diagnostics_max_bytes: int = Field(default=10_000_000, ge=1024, alias="DIAGNOSTICS_MAX_BYTES")
    diagnostics_backups: int = Field(default=5, ge=1, le=100, alias="DIAGNOSTICS_BACKUPS")
    model_input_price_usd: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, alias="MODEL_INPUT_PRICE_PER_MILLION_USD"
    )
    model_output_price_usd: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, alias="MODEL_OUTPUT_PRICE_PER_MILLION_USD"
    )
    model_price_reference: str | None = Field(default=None, alias="MODEL_PRICE_REFERENCE")
    api_prefix: str = "/api"
    app_version: str = "0.2.0"

    # БД приложения (метаданные, пользователи, чаты, дашборды)
    app_database_url: str = Field(default="sqlite:///./var/app.db", alias="APP_DATABASE_URL")
    results_data_dir: str = Field(default="./var/data/results", alias="RESULTS_DATA_DIR")
    datasets_data_dir: str = Field(default="./var/data/datasets", alias="DATASETS_DATA_DIR")
    source_encryption_key: SecretStr | None = Field(default=None, alias="SOURCE_ENCRYPTION_KEY")
    # Порог материализации ClickHouse (#61): суммарно по выбранным таблицам источника.
    clickhouse_max_rows: int = Field(default=10_000_000, ge=1, alias="CLICKHOUSE_MAX_ROWS")
    clickhouse_max_bytes: int = Field(default=1_000_000_000, ge=1, alias="CLICKHOUSE_MAX_BYTES")
    clickhouse_query_timeout: int = Field(
        default=300, ge=1, le=3600, alias="CLICKHOUSE_QUERY_TIMEOUT_SECONDS"
    )

    # Legacy bootstrap utility only; HTTP resource APIs always require verified sessions.
    cp1_local_mode: bool = Field(default=False, alias="CP1_LOCAL_MODE")
    cp1_local_user_id: str = Field(
        default="cp1-local-user", min_length=1, alias="CP1_LOCAL_USER_ID"
    )

    auth_rate_limit_requests: int = Field(default=10, ge=1, alias="AUTH_RATE_LIMIT_REQUESTS")
    auth_rate_limit_window_seconds: int = Field(
        default=300, ge=1, alias="AUTH_RATE_LIMIT_WINDOW_SECONDS"
    )

    # Согласованный управляемый профиль PL-1; совместимый endpoint можно переопределить.
    model_base_url: str = Field(
        default="https://api.deepinfra.com/v1/openai", alias="MODEL_BASE_URL"
    )
    model_name: str = Field(default="Qwen/Qwen3.6-35B-A3B", alias="MODEL_NAME")
    model_revision: str | None = Field(default=None, alias="MODEL_REVISION")
    # Ключ и адрес API из интерфейса (#209): только для локального запуска. На сервере
    # false: ключ не идёт через браузер, а данные — на произвольный хост.
    model_settings_from_ui: bool = Field(default=False, alias="MODEL_SETTINGS_FROM_UI")
    # Модели, которые можно выбрать в интерфейсе; MODEL_NAME разрешена всегда.
    model_allowed_names: list[str] | str = Field(default=[], alias="MODEL_ALLOWED_NAMES")
    # LLM descriptions of new dataset catalogs (B1-6); off unless enabled in .env.
    catalog_descriptions: bool = Field(default=False, alias="CATALOG_DESCRIPTIONS")

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

    @field_validator("model_allowed_names", mode="before")
    @classmethod
    def parse_model_names(cls, v: Any) -> list[str]:
        """Список через запятую или JSON-массив; пустое значение — пустой список."""
        if v is None:
            return []
        if isinstance(v, str):
            value = v.strip()
            if value.startswith("["):
                import json

                try:
                    parsed = json.loads(value)
                except ValueError as e:
                    raise ValueError(f"Некорректный JSON в MODEL_ALLOWED_NAMES: {e}") from e
                if not isinstance(parsed, list):
                    raise ValueError("MODEL_ALLOWED_NAMES в формате JSON должен быть массивом")
                items = parsed
            else:
                items = value.split(",")
        elif isinstance(v, (list, tuple, set)):
            items = list(v)
        else:
            items = [v]
        return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))

    @field_validator(
        "app_env",
        "log_level",
        "app_database_url",
        "results_data_dir",
        "datasets_data_dir",
        "source_encryption_key",
        "clickhouse_max_rows",
        "clickhouse_max_bytes",
        "clickhouse_query_timeout",
        "cp1_local_mode",
        "cp1_local_user_id",
        "auth_rate_limit_requests",
        "auth_rate_limit_window_seconds",
        "model_base_url",
        "model_name",
        "model_revision",
        "model_settings_from_ui",
        "sql_max_rows",
        "tool_timeout_seconds",
        "allowed_outbound_hosts",
        "session_secret",
        "max_concurrent_chats",
        "max_concurrent_chats_per_owner",
        "max_queued_chats",
        "max_queued_chats_per_owner",
        "chat_admission_timeout_seconds",
        "max_concurrent_dataset_jobs",
        "max_concurrent_dataset_jobs_per_owner",
        "max_queued_dataset_jobs",
        "max_queued_dataset_jobs_per_owner",
        "max_upload_file_bytes",
        "max_upload_request_bytes",
        "max_owner_storage_bytes",
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

    @model_validator(mode="after")
    def allow_configured_model(self) -> Settings:
        assert isinstance(self.model_allowed_names, list)
        if self.model_name not in self.model_allowed_names:
            self.model_allowed_names = [self.model_name, *self.model_allowed_names]
        return self

    @model_validator(mode="after")
    def complete_model_tariff(self) -> Settings:
        supplied = (
            self.model_input_price_usd is not None,
            self.model_output_price_usd is not None,
            self.model_price_reference is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "Задайте обе ставки MODEL_*_PRICE_PER_MILLION_USD и MODEL_PRICE_REFERENCE"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()
