"""Общие технические контракты API (проверка здоровья, ошибки, обертки ответов)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from djedai.contracts.catalog import DatasetCatalog, DatasetSource
from djedai.contracts.tool_result import ToolError


class HealthCheck(BaseModel):
    """Ответ эндпоинта проверки работоспособности сервиса."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(default="ok", description="Статус сервиса")
    app_env: str = Field(description="Окружение приложения (development, production)")
    version: str = Field(description="Версия приложения")


class ErrorResponse(BaseModel):
    """Стандартный формат ошибки API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ToolError = Field(description="Код, понятное сообщение и рекомендация")


class DatasetListSource(DatasetSource):
    """Источник в списке UI; files — метаданные загрузки, не поле каталога."""

    files: int | None = Field(default=None, ge=0)


class DatasetSummary(BaseModel):
    """Краткая сводка датасета для списков в API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(description="ID датасета")
    name: str = Field(description="Имя датасета")
    source: DatasetListSource
    status: Literal["processing", "ready", "error"]
    table_count: int | None = Field(ge=0)
    row_count: int | None = Field(ge=0)
    created_at: AwareDatetime = Field(description="Дата создания")


class DatasetDetail(DatasetSummary):
    catalog: DatasetCatalog | None
    error: ToolError | None


class DatasetUploadAccepted(BaseModel):
    """Acknowledgement of a persisted upload queued for profiling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    status: Literal["processing"] = "processing"
