"""Контракт Dashboard (§7.2) — сущность перевыполняемого дашборда."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class WidgetLayout(BaseModel):
    """Позиция и размеры виджета в сетке дашборда."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int = Field(ge=0, description="Позиция X в сетке")
    y: int = Field(ge=0, description="Позиция Y в сетке")
    w: int = Field(ge=1, description="Ширина виджета в колонках")
    h: int = Field(ge=1, description="Высота виджета в строках")


class WidgetToolCall(BaseModel):
    """Рецепт перевычисления данных виджета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(description="Имя инструмента, например 'time_aggregate'")
    params: dict[str, Any] = Field(default_factory=dict, description="Параметры вызова")


class DashboardWidget(BaseModel):
    """Виджет дашборда со схемой пересчета и визуализацией."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    widget_id: str = Field(description="Уникальный идентификатор виджета")
    tool_call: WidgetToolCall = Field(description="Вызов инструмента для получения свежих данных")
    chart: dict[str, Any] | None = Field(
        default=None, description="ChartSpec без result_id — подставляется при рендере"
    )
    caption: str | None = Field(default=None, description="Краткий текстовый вывод или инсайт")
    layout: WidgetLayout = Field(description="Расположение на сетке")


class Dashboard(BaseModel):
    """Сущность дашборда, сохраняемая в БД приложения и доступная для обновления."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dashboard_id: str = Field(description="Уникальный идентификатор дашборда")
    dataset_id: str = Field(description="ID датасета, к которому привязан дашборд")
    title: str = Field(description="Название дашборда")
    description: str | None = Field(default=None, description="Описание дашборда")
    source_chat_id: str | None = Field(
        default=None, description="ID чата, в котором был создан дашборд"
    )
    widgets: list[DashboardWidget] = Field(default_factory=list, description="Список виджетов")
    created_at: AwareDatetime = Field(description="Дата создания")
    refreshed_at: AwareDatetime | None = Field(
        default=None, description="Дата последнего пересчета"
    )
