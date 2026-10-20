"""Контракт ChartSpec (§6.2) — декларативное описание визуализации для фронтенда."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChartType = Literal[
    "line", "bar", "area", "scatter", "pie", "histogram", "heatmap", "box", "table", "kpi"
]
AxisKind = Literal["time", "category", "number"]
FormatKind = Literal["number", "money", "percent", "int"]
OrientationKind = Literal["vertical", "horizontal"]
SortOrder = Literal["asc", "desc"]


class XAxis(BaseModel):
    """Конфигурация оси X."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    label: str | None = None
    kind: AxisKind = "category"


class YMeasure(BaseModel):
    """Показатель оси Y с правилами числового форматирования."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    label: str | None = None
    format: FormatKind = "number"


class ColorField(BaseModel):
    """Поле цветовой разбивки (сегментации)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    label: str | None = None


class SortSpec(BaseModel):
    """Спецификация сортировки графика."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    order: SortOrder = "desc"


class Annotation(BaseModel):
    """Текстовая аннотация на графике (пик, событие)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: str
    text: str


class ChartTemplate(BaseModel):
    """Визуальные настройки, сохраняемые до первого успешного вычисления."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chart_id: str = Field(description="Уникальный идентификатор графика")
    type: ChartType = Field(description="Тип графика")
    title: str = Field(description="Понятный заголовок для бизнес-пользователя")
    subtitle: str | None = Field(default=None, description="Краткий вывод или подзаголовок")
    x: XAxis | None = Field(default=None, description="Ось X")
    y: list[YMeasure] = Field(default_factory=list, description="Меры по оси Y")
    color: ColorField | None = Field(default=None, description="Цветовая группировка")
    orientation: OrientationKind = Field(default="vertical", description="Ориентация столбцов")
    stacked: bool = Field(default=False, description="Наложение рядов")
    sort: SortSpec | None = Field(default=None, description="Сортировка")
    limit: int = Field(default=20, ge=1, description="Лимит отображаемых категорий/точек")
    annotations: list[Annotation] = Field(
        default_factory=list, description="Текстовые метки на точках"
    )


class ChartSpec(ChartTemplate):
    """Спецификация графика. Данные фронтенд запрашивает отдельно по result_id."""

    result_id: str = Field(description="ID результата вычислений в хранилище результатов")
