from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from djedai.contracts.chart import ChartSpec, ChartTemplate
from djedai.contracts.dashboard import DashboardSnapshot
from djedai.contracts.metrics import MetricDefinition, MetricSpec
from djedai.contracts.tool_result import ToolResult


class Entities(BaseModel):
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)


class Clarification(BaseModel):
    question: str
    options: list[str]
    # Кандидаты определений по порядку options: выбор варианта подтверждает определение (B2-9)
    metric_candidates: list[MetricSpec] = Field(default_factory=list)


class Understanding(BaseModel):
    intent: Literal[
        "data_overview",
        "data_question",
        "visualize",
        "modify_previous",
        "dashboard",
        "hypothesis",
        "ambiguous",
        "invalid",
        "off_topic",
    ]
    resolved_question: str | None = None
    entities: Entities = Field(default_factory=Entities)
    references: list[str] = Field(default_factory=list)
    # Сохранённая панель, которую пользователь просит открыть или обновить.
    dashboard_id: str | None = None
    clarification: Clarification | None = None


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class FastPath(BaseModel):
    """Узкий детерминированный маршрут без вызова модели.

    ``table_id`` уже проверен по текущему каталогу датасета: имя таблицы
    разрешено однозначно, а не угадано по тексту вопроса.
    """

    kind: Literal["row_count", "structure"]
    table_id: str


class PlannedCall(BaseModel):
    tool: str
    args: dict[str, Any]


class Block(BaseModel):
    """Блок ответа агента до преобразования в контракт MessageBlock."""

    type: str
    markdown: str | None = None
    result_id: str | None = None
    columns: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1)
    chart: dict[str, Any] | None = None
    options: list[str] = Field(default_factory=list)
    dashboard_id: str | None = None
    title: str | None = None
    items: list[str] = Field(default_factory=list)
    # clarification: проверенный кандидат определения для варианта с тем же индексом (B2-9)
    metric_candidates: list[MetricDefinition | None] = Field(default_factory=list)


class PlanResponse(BaseModel):
    # Enforces the max 5 steps requirement
    plan: list[PlannedCall] = Field(max_length=5)


class DashboardStepPlan(BaseModel):
    """Один вызов инструмента внутри вычисления виджета дашборда."""

    step_id: str = Field(min_length=1, description="Идентификатор шага внутри виджета")
    tool: str = Field(min_length=1, description="Имя инструмента из реестра")
    args: dict[str, Any] = Field(description="Параметры вызова")
    source_step_id: str | None = Field(
        default=None,
        description="Предыдущий шаг этого же виджета; его данные доступны как source_result",
    )


class DashboardWidgetPlan(BaseModel):
    """Виджет дашборда до выполнения: график и один или несколько шагов расчёта."""

    title: str = Field(min_length=1, description="Понятный заголовок виджета")
    chart: ChartTemplate = Field(description="Настройки графика без временного result_id")
    caption: str | None = Field(default=None, description="Короткий вывод под заголовком")
    steps: list[DashboardStepPlan] = Field(min_length=1, max_length=5)
    output_step_id: str | None = Field(
        default=None,
        description="Шаг, чей результат показывает виджет; по умолчанию последний",
    )


class DashboardPlanResponse(BaseModel):
    """План дашборда: от 3 до 8 независимо перевыполняемых виджетов."""

    title: str = Field(min_length=1, description="Название дашборда")
    description: str | None = Field(default=None, description="Краткое описание дашборда")
    widgets: list[DashboardWidgetPlan] = Field(min_length=3, max_length=8)


class AgentState(TypedDict, total=False):
    chat_id: str
    dataset_id: str
    messages: list[Message]
    catalog_prompt: str
    # Normalized column names whose original header is sensitive; keeps
    # `dbPasswordText` sensitive after normalization to `dbpasswordtext`.
    sensitive_columns: tuple[str, ...]
    # В каталоге нет ни одного признака денежных полей: выручку не из чего считать.
    money_fields_absent: bool
    history_summary: str | None
    user_context: str | None
    understanding: Understanding | None
    # Узкий быстрый маршрут текущего хода; None — обычный маршрут с моделью.
    fast_path: FastPath | None
    plan: list[PlannedCall]
    results: list[ToolResult]
    # Results saved by previous turns; kept aside so a view change can reuse them
    # without recalculating and without polluting the current turn's facts.
    history_results: list[ToolResult]
    focus_result_ids: list[str]
    # Blocks of the latest assistant response: "the second point" resolves to a
    # specific table/chart without recalculating the data behind it.
    previous_blocks: list[Block]
    charts: list[ChartSpec]
    answer_blocks: list[Block]
    retries: int
    compose_retries: int
    is_verified: bool
    # Отдельная ветка дашборда: план виджетов, их вычисления и сохранённый снимок.
    dashboard_plan: DashboardPlanResponse | None
    dashboard_widgets: list[Any]
    dashboard_snapshot: DashboardSnapshot | None
    dashboard_retries: int


class ComposeResponse(BaseModel):
    answer_blocks: list[Block]


class VerifyResponse(BaseModel):
    is_valid: bool
    issues: list[str] = Field(default_factory=list)
    corrected_markdown: str


class VisualizeResponse(BaseModel):
    charts: list[ChartSpec] = Field(default_factory=list)
