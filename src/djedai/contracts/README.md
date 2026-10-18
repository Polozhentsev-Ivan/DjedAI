# Общие контракты

Единые Pydantic v2 модели для обмена данными между компонентами платформы (B5-1, PL-1).

## Модели пакета

- `catalog.py` — `DatasetCatalog`, `Table`, `Column`, `Relation`, `Transformation` (§3.4);
- `tool_result.py` — `ToolResult`, `Fact`, `ChartHint`, `ToolError`, `ToolResultData` (§4.3);
- `chart.py` — `ChartSpec`, `XAxis`, `YMeasure`, `ColorField` (§6.2);
- `message.py` — `AssistantMessage`, `TextBlock`, `ChartBlock`, `TableBlock`, `ClarificationBlock`, `DashboardLinkBlock` (§5.6);
- `dashboard.py` — `Dashboard`, `DashboardWidget`, `WidgetToolCall`, `WidgetLayout` (§7.2);
- `auth.py` — `UserCreate`, `UserLogin`, `UserRead`, `TokenResponse`;
- `common.py` — `HealthCheck`, `ErrorResponse`, `DatasetSummary`.

Все модели экспортируются напрямую из пакета `djedai.contracts`.
