from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from djedai.contracts.tool_result import ToolResult


class Entities(BaseModel):
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)


class Clarification(BaseModel):
    question: str
    options: list[str]


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
    clarification: Clarification | None = None


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class PlannedCall(BaseModel):
    tool: str
    args: dict[str, Any]


class ChartSpec(BaseModel):
    title: str
    type: str
    data_ref: str


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


class AgentState(TypedDict, total=False):
    chat_id: str
    dataset_id: str
    messages: list[Message]
    catalog_prompt: str
    history_summary: str | None
    user_context: str | None
    understanding: Understanding | None
    plan: list[PlannedCall]
    results: list[ToolResult]
    focus_result_ids: list[str]
    charts: list[ChartSpec]
    answer_blocks: list[Block]
    retries: int


class PlanResponse(BaseModel):
    # Enforces the max 5 steps requirement
    plan: list[PlannedCall] = Field(max_length=5)


class ComposeResponse(BaseModel):
    answer_blocks: list[Block]
