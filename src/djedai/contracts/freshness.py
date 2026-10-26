"""Свежесть источника и происхождение чтения расчёта (B1-11, #169).

``SourceFreshness`` сравнивает состояние источника с ревизией каталога: совпала ли
схема и не устарел ли профиль. Правила и согласованность нескольких чтений описаны в
``docs/decisions/169-catalog-revision.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

FreshnessState = Literal["fresh", "aged", "drifted", "incompatible", "unknown"]
ChangeKind = Literal["dropped_table", "added_column", "dropped_column", "type_changed"]
COMPATIBLE_CHANGES: frozenset[ChangeKind] = frozenset({"added_column"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaChange(_Model):
    kind: ChangeKind
    table: str = Field(min_length=1)  # таблица в источнике: schema.table или имя raw
    column: str | None = None
    before: str | None = None  # тип DuckDB в каталоге
    after: str | None = None  # тип DuckDB в источнике сейчас

    @property
    def compatible(self) -> bool:
        return self.kind in COMPATIBLE_CHANGES

    def describe(self) -> str:
        where = f"{self.table}.{self.column}" if self.column else self.table
        match self.kind:
            case "dropped_table":
                return f"таблица {where} удалена"
            case "added_column":
                return f"добавлена колонка {where}"
            case "dropped_column":
                return f"колонка {where} удалена или переименована"
            case "type_changed":
                return f"тип {where} изменился: {self.before} → {self.after}"


class SourceFreshness(_Model):
    state: FreshnessState
    profiled_at: AwareDatetime | None = None  # из ревизии каталога; None — ревизии нет
    checked_at: AwareDatetime | None = None  # когда схему сравнили с живым источником
    changes: list[SchemaChange] = Field(default_factory=list)
    reason: str | None = None  # почему состояние не fresh, понятно пользователю

    @model_validator(mode="after")
    def _state_is_explained(self) -> SourceFreshness:
        compatible = all(change.compatible for change in self.changes)
        if self.state in ("drifted", "incompatible") and not self.changes:
            raise ValueError(f"{self.state}: нужен список изменений схемы")
        if self.state == "drifted" and not compatible:
            raise ValueError("drifted допускает только совместимые изменения")
        if self.state == "incompatible" and compatible:
            raise ValueError("incompatible требует несовместимого изменения")
        if self.state in ("fresh", "aged", "unknown") and self.changes:
            raise ValueError(f"{self.state}: изменений схемы быть не должно")
        if self.state in ("aged", "unknown") and not self.reason:
            raise ValueError(f"{self.state}: нужна причина")
        if self.state != "unknown" and self.profiled_at is None:
            raise ValueError(f"{self.state}: нужен profiled_at ревизии")
        return self


class DatasetRefresh(_Model):
    """Идущая или неудавшаяся перепрофилировка; датасет при этом остаётся ready."""

    status: Literal["processing", "error"]
    started_at: AwareDatetime
    message: str | None = None  # текст ошибки без DSN и трассировки
