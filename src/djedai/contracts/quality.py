"""Контракт качества преобразования колонок, на которых основан расчёт (B2-10, #168).

Typed view превращает нераспознанное значение в NULL, и SUM по нему молча становится
неполным. ``QualityReport`` связывает результат инструмента с колонками, которые
реально использовал его SQL (lineage через JOIN, CTE и производные выражения), и для
каждой показывает: исходную таблицу и колонку, преобразование, знаменатель, исходные
пропуски отдельно от ошибок разбора и найденные ограничения.

Числа считаются на популяции расчёта (``scope="population"``: после JOIN и WHERE, до
GROUP BY). Если это невозможно, берётся профиль всей таблицы (``scope="table"``), а
``boundary`` объясняет, почему точного числа нет. Правила описаны в
``docs/decisions/168-typing-losses.md``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from djedai.contracts.catalog import MAX_SAMPLE_VALUES, SemanticType

QualityCode = Literal[
    "parse_losses",  # непустые значения не распознаны и в расчёте стали NULL
    "kept_as_text",  # колонка не приведена к типу (ниже порога или по другой причине)
    "ambiguous_date",  # день и месяц не различить: формат не выбран или выбран большинством
    "ambiguous_separator",  # одиночный разделитель принят за десятичный (1,234)
    "mixed_currency",  # несколько валют в колонке или в одном выражении
    "percent_scale",  # 15% и 0.15 в одной колонке: масштаб значений без знака неизвестен
    "percent_sum",  # сумма процентов не имеет смысла доли
]
Severity = Literal["info", "warning", "blocking"]
Usage = Literal["measure", "filter", "join_key", "group", "output"]
QualityScope = Literal["population", "table"]
Count = Annotated[int, Field(ge=0)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]

SEVERITY_ORDER: dict[Severity, int] = {"info": 0, "warning": 1, "blocking": 2}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualityIssue(_Model):
    code: QualityCode
    severity: Severity
    message: str = Field(min_length=1)  # понятное пользователю ограничение с числами
    # варианты уточнения для blocking; каждый начинается с «таблица.колонка:», и ответ
    # пользователя с этой ссылкой снимает блокировку
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _blocking_has_options(self) -> QualityIssue:
        if self.severity == "blocking" and len(self.options) < 2:
            raise ValueError("blocking-ограничению нужны минимум два варианта уточнения")
        return self


class ColumnQuality(_Model):
    """Качество одной колонки typed view в популяции конкретного расчёта."""

    table: str  # typed view
    column: str
    raw_table: str | None = None  # None — живой источник, типы задала БД
    original_name: str
    transformation: str  # действие журнала: parse_money, parse_date, keep_text, …
    target_type: SemanticType | None = None  # тип, к которому приводили значения
    format: str | None = None
    usage: list[Usage] = Field(min_length=1)
    scope: QualityScope
    boundary: str | None = None  # граница знания для scope="table"
    rows: Count  # знаменатель: строки популяции (scope="table" — вся таблица)
    source_empty: Count  # исходные NULL и пустые строки — не ошибки разбора
    non_empty: Count
    parse_failed: Count
    failed_ratio: Ratio  # parse_failed / non_empty
    examples: list[str] = Field(default_factory=list, max_length=MAX_SAMPLE_VALUES)
    issues: list[QualityIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> ColumnQuality:
        if self.source_empty + self.non_empty != self.rows:
            raise ValueError(f"{self.table}.{self.column}: пустые + непустые ≠ строки")
        if self.parse_failed > self.non_empty:
            raise ValueError(f"{self.table}.{self.column}: ошибок разбора больше непустых")
        if (self.scope == "table") != (self.boundary is not None):
            raise ValueError(f"{self.table}.{self.column}: boundary задаётся только для table")
        return self

    @property
    def severity(self) -> Severity | None:
        return max((i.severity for i in self.issues), key=SEVERITY_ORDER.get, default=None)


class QualityReport(_Model):
    """Ограничения данных, на которых основан результат инструмента."""

    lineage: Literal["complete", "partial"]
    unresolved: list[str] = Field(default_factory=list)  # что не связано с колонками каталога
    checked: list[str] = Field(default_factory=list)  # «таблица.колонка» всех проверенных
    columns: list[ColumnQuality] = Field(default_factory=list)  # только колонки с issues

    @model_validator(mode="after")
    def _partial_names_reason(self) -> QualityReport:
        if (self.lineage == "partial") != bool(self.unresolved):
            raise ValueError("unresolved задаётся только для неполной lineage")
        return self

    @property
    def issues(self) -> list[QualityIssue]:
        return [issue for column in self.columns for issue in column.issues]

    def has(self, severity: Severity) -> bool:
        return any(SEVERITY_ORDER[i.severity] >= SEVERITY_ORDER[severity] for i in self.issues)
