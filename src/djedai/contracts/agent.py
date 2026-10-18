from typing import Literal

from pydantic import BaseModel, Field


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
