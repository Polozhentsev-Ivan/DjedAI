"""Пакет общих согласованных контрактов данных DjedAI.

Экспортирует единые Pydantic v2 модели для API, аналитических функций,
графа агента и пользовательского интерфейса.
"""

from __future__ import annotations

from djedai.contracts.auth import TokenResponse, UserBase, UserCreate, UserLogin, UserRead
from djedai.contracts.catalog import (
    Column,
    ColumnStats,
    DatasetCatalog,
    DatasetSource,
    ParseInfo,
    Relation,
    Table,
    TableSource,
    TopValue,
    Transformation,
)
from djedai.contracts.chart import (
    Annotation,
    ChartSpec,
    ChartType,
    ColorField,
    SortSpec,
    XAxis,
    YMeasure,
)
from djedai.contracts.common import DatasetSummary, ErrorResponse, HealthCheck
from djedai.contracts.dashboard import (
    Dashboard,
    DashboardWidget,
    WidgetLayout,
    WidgetToolCall,
)
from djedai.contracts.message import (
    AssistantMessage,
    ChartBlock,
    ClarificationBlock,
    DashboardLinkBlock,
    MessageBlock,
    SuggestionsBlock,
    TableBlock,
    TextBlock,
    TraceStep,
)
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultColumn,
    ToolResultData,
)

__all__ = [
    # Каталог
    "Column",
    "ColumnStats",
    "DatasetCatalog",
    "DatasetSource",
    "ParseInfo",
    "Relation",
    "Table",
    "TableSource",
    "TopValue",
    "Transformation",
    # Результаты инструментов
    "ChartHint",
    "Fact",
    "ToolError",
    "ToolResult",
    "ToolResultColumn",
    "ToolResultData",
    # Визуализация
    "Annotation",
    "ChartSpec",
    "ChartType",
    "ColorField",
    "SortSpec",
    "XAxis",
    "YMeasure",
    # Чат и сообщения агента
    "AssistantMessage",
    "ChartBlock",
    "ClarificationBlock",
    "DashboardLinkBlock",
    "MessageBlock",
    "SuggestionsBlock",
    "TableBlock",
    "TextBlock",
    "TraceStep",
    # Дашборды
    "Dashboard",
    "DashboardWidget",
    "WidgetLayout",
    "WidgetToolCall",
    # Авторизация
    "TokenResponse",
    "UserBase",
    "UserCreate",
    "UserLogin",
    "UserRead",
    # Общие технические
    "DatasetSummary",
    "ErrorResponse",
    "HealthCheck",
]
