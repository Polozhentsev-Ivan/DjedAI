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
from djedai.contracts.chat import (
    ChatCreate,
    ChatCreated,
    ChatDetail,
    ChatMessage,
    ChatSummary,
    SendMessageRequest,
)
from djedai.contracts.common import DatasetDetail, DatasetSummary, ErrorResponse, HealthCheck
from djedai.contracts.dashboard import (
    Dashboard,
    DashboardSnapshot,
    DashboardWidget,
    WidgetLayout,
    WidgetRecipe,
    WidgetRecipeStep,
    WidgetSnapshot,
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
from djedai.contracts.sse import (
    AgentStreamEvent,
    DatasetDoneData,
    DatasetDoneEvent,
    DatasetErrorEvent,
    DatasetProgressData,
    DatasetProgressEvent,
    DatasetStreamEvent,
    SSEBlockEvent,
    SSEDoneData,
    SSEDoneEvent,
    SSEErrorData,
    SSEErrorEvent,
    SSEStatusData,
    SSEStatusEvent,
    SSEToolCallData,
    SSEToolCallEvent,
    SSEToolResultData,
    SSEToolResultEvent,
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
    # Чаты
    "ChatCreate",
    "ChatCreated",
    "ChatDetail",
    "ChatMessage",
    "ChatSummary",
    "SendMessageRequest",
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
    "DashboardSnapshot",
    "DashboardWidget",
    "WidgetLayout",
    "WidgetRecipe",
    "WidgetRecipeStep",
    "WidgetSnapshot",
    "WidgetToolCall",
    # Авторизация
    "TokenResponse",
    "UserBase",
    "UserCreate",
    "UserLogin",
    "UserRead",
    # Общие технические
    "DatasetSummary",
    "DatasetDetail",
    "ErrorResponse",
    "HealthCheck",
    # SSE события стриминга (§7.3)
    "AgentStreamEvent",
    "DatasetDoneData",
    "DatasetDoneEvent",
    "DatasetErrorEvent",
    "DatasetProgressData",
    "DatasetProgressEvent",
    "DatasetStreamEvent",
    "SSEBlockEvent",
    "SSEDoneData",
    "SSEDoneEvent",
    "SSEErrorData",
    "SSEErrorEvent",
    "SSEStatusData",
    "SSEStatusEvent",
    "SSEToolCallData",
    "SSEToolCallEvent",
    "SSEToolResultData",
    "SSEToolResultEvent",
]
