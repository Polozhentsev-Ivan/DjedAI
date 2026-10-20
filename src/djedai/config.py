"""Общие лимиты data/analytics/backend из окружения и локального .env (PL-1)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model_timeout_seconds: float = Field(default=60, gt=0, alias="MODEL_TIMEOUT_SECONDS")
    model_max_output_tokens: int = Field(default=2048, ge=1, alias="MODEL_MAX_OUTPUT_TOKENS")
    model_context_tokens: int = Field(default=8192, ge=1, alias="MODEL_CONTEXT_TOKENS")

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
        return self


@lru_cache
def get_limits() -> LimitSettings:
    """Лимиты одного процесса; изменение окружения требует перезапуска."""
    return LimitSettings()
