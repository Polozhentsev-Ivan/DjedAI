"""Общие технические контракты API (проверка здоровья, ошибки, обертки ответов)."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class HealthCheck(BaseModel):
    """Ответ эндпоинта проверки работоспособности сервиса."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(default="ok", description="Статус сервиса")
    app_env: str = Field(description="Окружение приложения (development, production)")
    version: str = Field(description="Версия приложения")


class ErrorResponse(BaseModel):
    """Стандартный формат ошибки API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(description="Символьный код ошибки")
    message: str = Field(description="Понятное описание ошибки")
    details: Any | None = Field(default=None, description="Дополнительные детали")


class DatasetSummary(BaseModel):
    """Краткая сводка датасета для списков в API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(description="ID датасета")
    name: str = Field(description="Имя датасета")
    source_kind: str = Field(description="Тип источника (csv_upload, postgres, clickhouse)")
    status: str = Field(description="Статус (ready, profiling, error)")
    created_at: AwareDatetime = Field(description="Дата создания")
