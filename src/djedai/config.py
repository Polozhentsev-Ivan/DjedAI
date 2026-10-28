"""Общие лимиты data/analytics/backend из окружения и локального .env (PL-1)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Application-supported subset of DeepInfra reasoning_effort values.
# provider_default additionally omits the parameter and keeps the provider/model
# default behaviour.
ReasoningEffort = Literal["provider_default", "none", "low", "medium", "high"]


class LimitSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        allow_inf_nan=False,
    )

    type_parse_threshold: float = Field(default=0.95, gt=0, le=1, alias="TYPE_PARSE_THRESHOLD")
    relation_min_overlap: float = Field(default=0.5, gt=0, le=1, alias="RELATION_MIN_OVERLAP")
    relation_unnamed_min_overlap: float = Field(
        default=0.95, gt=0, le=1, alias="RELATION_UNNAMED_MIN_OVERLAP"
    )
    relation_min_jaccard: float = Field(default=0.5, gt=0, le=1, alias="RELATION_MIN_JACCARD")
    sql_max_rows: int = Field(default=5000, ge=1, alias="SQL_MAX_ROWS")
    tool_timeout_seconds: float = Field(default=30, gt=0, alias="TOOL_TIMEOUT_SECONDS")
    dashboard_max_tool_calls: int = Field(default=20, ge=1, alias="DASHBOARD_MAX_TOOL_CALLS")
    # Профиль живого PostgreSQL старше порога — ограничение «aged» в ответе (B1-11)
    catalog_max_age_hours: float = Field(default=24, gt=0, alias="CATALOG_MAX_AGE_HOURS")
    model_timeout_seconds: float = Field(default=60, gt=0, le=600, alias="MODEL_TIMEOUT_SECONDS")
    agent_turn_timeout_seconds: float = Field(
        default=120, gt=0, le=600, alias="AGENT_TURN_TIMEOUT_SECONDS"
    )
    model_max_retries: int = Field(default=1, ge=0, le=3, alias="MODEL_MAX_RETRIES")
    model_retry_delay_seconds: float = Field(
        default=0.5, ge=0, le=10, alias="MODEL_RETRY_DELAY_SECONDS"
    )
    model_max_output_tokens: int = Field(default=2048, ge=1, alias="MODEL_MAX_OUTPUT_TOKENS")
    model_context_tokens: int = Field(default=8192, ge=1, alias="MODEL_CONTEXT_TOKENS")
    # Reasoning is selected per stage. provider_default omits reasoning_effort
    # entirely, so the previous profile keeps the provider/model default.
    model_reasoning_understand: ReasoningEffort = Field(
        default="provider_default", alias="MODEL_REASONING_UNDERSTAND"
    )
    model_reasoning_plan: ReasoningEffort = Field(
        default="provider_default", alias="MODEL_REASONING_PLAN"
    )
    model_reasoning_visualize: ReasoningEffort = Field(
        default="provider_default", alias="MODEL_REASONING_VISUALIZE"
    )
    model_reasoning_compose: ReasoningEffort = Field(
        default="provider_default", alias="MODEL_REASONING_COMPOSE"
    )
    model_reasoning_verify: ReasoningEffort = Field(
        default="provider_default", alias="MODEL_REASONING_VERIFY"
    )
    # DeepInfra accepts reasoning_effort only for models that expose reasoning.
    # Default false: a non-default stage setting then fails at startup instead
    # of being silently ignored by an endpoint/model that does not support it.
    # Enable only after checking the selected model and endpoint.
    model_reasoning_supported: bool = Field(default=False, alias="MODEL_REASONING_SUPPORTED")

    # Лимиты параллельных вычислений и очередей (CP2 / #172)
    max_concurrent_chats: int = Field(default=4, ge=1, alias="MAX_CONCURRENT_CHATS")
    max_concurrent_chats_per_owner: int = Field(
        default=1, ge=1, alias="MAX_CONCURRENT_CHATS_PER_OWNER"
    )
    max_queued_chats: int = Field(default=10, ge=0, alias="MAX_QUEUED_CHATS")
    max_queued_chats_per_owner: int = Field(default=2, ge=0, alias="MAX_QUEUED_CHATS_PER_OWNER")
    chat_admission_timeout_seconds: float = Field(
        default=10.0, gt=0, alias="CHAT_ADMISSION_TIMEOUT_SECONDS"
    )

    max_concurrent_dataset_jobs: int = Field(default=2, ge=1, alias="MAX_CONCURRENT_DATASET_JOBS")
    max_concurrent_dataset_jobs_per_owner: int = Field(
        default=1, ge=1, alias="MAX_CONCURRENT_DATASET_JOBS_PER_OWNER"
    )
    max_queued_dataset_jobs: int = Field(default=10, ge=0, alias="MAX_QUEUED_DATASET_JOBS")
    max_queued_dataset_jobs_per_owner: int = Field(
        default=2, ge=0, alias="MAX_QUEUED_DATASET_JOBS_PER_OWNER"
    )

    # Лимиты загрузки файлов и дисковой квоты (CP2 / #172)
    max_upload_file_bytes: int = Field(default=50_000_000, ge=1, alias="MAX_UPLOAD_FILE_BYTES")
    max_upload_request_bytes: int = Field(
        default=100_000_000, ge=1, alias="MAX_UPLOAD_REQUEST_BYTES"
    )
    max_owner_storage_bytes: int = Field(default=500_000_000, ge=1, alias="MAX_OWNER_STORAGE_BYTES")

    @field_validator("*", mode="before")
    @classmethod
    def blank_uses_default(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @model_validator(mode="after")
    def validate_budgets(self) -> LimitSettings:
        if self.model_max_output_tokens >= self.model_context_tokens:
            raise ValueError("В контексте модели должно оставаться место для входного запроса")
        if self.relation_unnamed_min_overlap < self.relation_min_overlap:
            raise ValueError("Порог без совпадения имён не может быть слабее общего overlap")
        if self.max_upload_file_bytes > self.max_upload_request_bytes:
            raise ValueError("Лимит отдельного файла не может превышать лимит запроса")
        if self.max_upload_request_bytes > self.max_owner_storage_bytes:
            raise ValueError("Лимит запроса загрузки не может превышать квоту хранилища владельца")
        if self.max_concurrent_chats_per_owner > self.max_concurrent_chats:
            raise ValueError("Лимит чатов на владельца не может превышать общий лимит чатов")
        if self.max_concurrent_dataset_jobs_per_owner > self.max_concurrent_dataset_jobs:
            raise ValueError(
                "Лимит задач датасета на владельца не может превышать общий лимит задач"
            )
        return self


@lru_cache
def get_limits() -> LimitSettings:
    """Лимиты одного процесса; изменение окружения требует перезапуска."""
    return LimitSettings()
