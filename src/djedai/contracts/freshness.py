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


ReadMode = Literal["live", "snapshot", "saved_result", "unknown"]
Consistency = Literal["turn_snapshot", "independent", "immutable", "unknown"]
UNKNOWN_REASON = "Версия каталога и время чтения этого результата не сохранены: версия неизвестна"


class ReadProvenance(_Model):
    """Что и когда прочитал расчёт; копия ревизии, которую не меняет перепрофилировка."""

    revision_known: bool
    dataset_id: str | None = None
    catalog_revision: int | None = Field(default=None, ge=1)
    schema_fingerprint: str | None = None
    source_fingerprint: str | None = None
    transform_version: str | None = None
    read_mode: ReadMode
    # turn_snapshot — один снимок PostgreSQL на ход; immutable — файл датасета не меняется;
    # independent — у каждого чтения своё состояние источника
    consistency: Consistency
    snapshot_id: str | None = None  # чтения с одинаковым id видели одно состояние источника
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    freshness: SourceFreshness

    @classmethod
    def unknown(cls, reason: str = UNKNOWN_REASON) -> ReadProvenance:
        """Явное «версия неизвестна»: результат сохранён до #169 или без учёта чтения."""
        return cls(
            revision_known=False,
            read_mode="unknown",
            consistency="unknown",
            freshness=SourceFreshness(state="unknown", reason=reason),
        )

    @model_validator(mode="after")
    def _is_consistent(self) -> ReadProvenance:
        revision = (self.catalog_revision, self.schema_fingerprint, self.source_fingerprint)
        if self.revision_known != all(value is not None for value in revision):
            raise ValueError("revision_known требует ревизии и обоих отпечатков, и только их")
        if (self.read_mode == "unknown") != (self.started_at is None):
            raise ValueError("время чтения неизвестно только у read_mode=unknown")
        if (self.read_mode == "unknown") != (self.consistency == "unknown"):
            raise ValueError("согласованность неизвестна только у read_mode=unknown")
        if self.read_mode == "unknown" and self.revision_known:
            raise ValueError("read_mode=unknown только у результатов без ревизии")
        if self.started_at and (self.finished_at is None or self.finished_at < self.started_at):
            raise ValueError("finished_at не раньше started_at")
        if self.consistency in ("turn_snapshot", "immutable") and not self.snapshot_id:
            raise ValueError(f"{self.consistency}: нужен snapshot_id")
        if self.consistency == "independent" and self.snapshot_id:
            raise ValueError("independent: у чтения нет общего снимка")
        return self
