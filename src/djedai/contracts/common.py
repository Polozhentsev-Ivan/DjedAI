"""Общие технические контракты API (проверка здоровья, ошибки, обертки ответов)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from djedai.contracts.catalog import CatalogRevision, DatasetCatalog, DatasetSource
from djedai.contracts.freshness import DatasetRefresh, SourceFreshness
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
    revision: CatalogRevision | None = Field(
        default=None, description="Ревизия каталога; null — ещё не собран или собран до #169"
    )
    freshness: SourceFreshness | None = Field(
        default=None, description="Последняя проверка источника относительно каталога"
    )
    refresh: DatasetRefresh | None = Field(
        default=None, description="Идущая или неудавшаяся перепрофилировка"
    )


class DatasetDetail(DatasetSummary):
    catalog: DatasetCatalog | None
    error: ToolError | None


class DatasetUploadAccepted(BaseModel):
    """Acknowledgement of a persisted upload queued for profiling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    status: Literal["processing"] = "processing"


class ConnectRequest(BaseModel):
    """Owned database source; credentials are write-only and never part of its catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["postgres", "clickhouse"]
    dsn: SecretStr = Field(min_length=1, max_length=8192)
    tables: list[str] | None = Field(default=None, max_length=100)


class SourceCheckRequest(BaseModel):
    """Database source to probe before connecting; nothing is stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["postgres", "clickhouse"]
    dsn: SecretStr = Field(min_length=1, max_length=8192)


class SourceTable(BaseModel):
    """A table offered for connection; ``name`` is passed back in ``ConnectRequest.tables``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    row_count: int | None = Field(
        default=None, ge=0, description="Оценка числа строк, если источник её знает"
    )


class SourceCheckResult(BaseModel):
    """A reachable read-only source and its tables; the DSN and password are not echoed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["postgres", "clickhouse"]
    source: str = Field(description="Адрес без пароля: user@host:port/database")
    tables: list[SourceTable]
    truncated: bool = Field(default=False, description="Таблиц больше, чем показано")
