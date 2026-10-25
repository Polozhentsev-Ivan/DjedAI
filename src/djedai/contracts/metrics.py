"""Контракт определения бизнес-метрики (B2-9, #166).

Определение фиксирует смысл метрики, которую подтвердил пользователь: grain (таблица и ключ
строки), формулу из ограниченного набора агрегатов, популяцию (фильтры), единицу и валюту,
колонку календаря и для отношений — числитель и знаменатель. Формула задаётся структурой,
а не SQL: запрос собирает ``djedai.analytics.metrics`` и сам проверяет grain JOIN-ов.

Имя колонки, описание LLM и ``confidence`` связи определения не утверждают: такое
определение может быть только ``proposed``. В расчёт идут лишь ``confirmed`` версии с
``confirmation``. Нет подтверждённой версии — метрика не определена, агент уточняет вопрос.
Решения и границы описаны в ``docs/decisions/166-metric-semantics.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from djedai.contracts.catalog import (
    NUMERIC,
    TEMPORAL,
    Column,
    ColumnRef,
    CurrencyCode,
    DatasetCatalog,
    Relation,
    Value,
)

MetricKind = Literal["sum", "count", "count_distinct", "ratio"]
MetricStatus = Literal["proposed", "confirmed", "retired"]
MetricUnit = Literal["money", "count", "ratio", "number"]
# additive — значения групп и периодов можно складывать; recompute — только пересчитывать
# по исходным строкам (уникальные клиенты, доли)
Additivity = Literal["additive", "recompute"]
FilterOp = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null"]
ConfirmationSource = Literal["api", "clarification"]
MetricId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Count = Annotated[int, Field(ge=0)]

_ADDITIVITY: dict[MetricKind, Additivity] = {
    "sum": "additive",
    "count": "additive",
    "count_distinct": "recompute",
    "ratio": "recompute",
}
_UNITS: dict[MetricKind, tuple[MetricUnit, ...]] = {
    "sum": ("money", "number"),
    "count": ("count",),
    "count_distinct": ("count",),
    "ratio": ("ratio",),
}
_LIST_OPS = ("in", "not_in")
_NULL_OPS = ("is_null", "not_null")


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class MetricGrain(_Model):
    """Одна строка метрики — одна строка ``table`` с уникальным ``key``."""

    table: str = Field(min_length=1)
    key: str = Field(min_length=1)


class MetricFilter(_Model):
    """Условие популяции; колонка — из таблицы grain или из таблицы по связи N:1 от неё."""

    column: ColumnRef
    op: FilterOp
    value: Value | list[Value] | None = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> MetricFilter:
        if self.op in _NULL_OPS:
            if self.value is not None:
                raise ValueError(f"{self.op}: значение не нужно")
        elif self.op in _LIST_OPS:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.op}: нужен непустой список значений")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.op}: нужно одно значение")
        return self


class MetricConfirmation(_Model):
    """Кто и как подтвердил определение: через API или ответом на уточнение агента."""

    source: ConfirmationSource
    by_user_id: str = Field(min_length=1)
    at: AwareDatetime
    message_id: str | None = None  # сообщение с уточнением, если source="clarification"


class MetricSpec(_Model):
    """Смысл метрики без версии и подтверждения — так кандидата предлагает агент."""

    metric_id: MetricId
    title: str = Field(min_length=1)
    description: str | None = None
    kind: MetricKind
    grain: MetricGrain
    measure: ColumnRef | None = None  # sum и count_distinct; count считает строки grain
    # ratio: metric_id подтверждённых sum/count; grain отношения — grain числителя
    numerator: MetricId | None = None
    denominator: MetricId | None = None
    additivity: Additivity
    filters: list[MetricFilter] = Field(default_factory=list)
    unit: MetricUnit
    currency: CurrencyCode | None = None  # одна валюта всех значений
    currency_column: ColumnRef | None = None  # или колонка с кодом валюты строки
    time_column: ColumnRef | None = None  # календарь: период [start, end) по этой колонке

    @model_validator(mode="after")
    def _fields_match_kind(self) -> MetricSpec:
        name = self.metric_id
        if self.kind == "ratio":
            if self.numerator is None or self.denominator is None:
                raise ValueError(f"{name}: у отношения нужны числитель и знаменатель")
            if self.numerator == self.denominator or name in (self.numerator, self.denominator):
                raise ValueError(f"{name}: числитель и знаменатель — разные метрики")
            if self.measure is not None:
                raise ValueError(f"{name}: у отношения нет своей колонки measure")
            if self.filters or self.time_column is not None:
                # популяция и календарь у каждой составляющей свои: общий фильтр
                # по-разному применялся бы к числителю и знаменателю
                raise ValueError(f"{name}: фильтры и календарь задаются в составляющих")
        else:
            if self.numerator is not None or self.denominator is not None:
                raise ValueError(f"{name}: числитель и знаменатель есть только у отношения")
            if self.kind != "count" and self.measure is None:
                raise ValueError(f"{name}: для {self.kind} нужна колонка measure")
            if self.kind == "count" and self.measure is not None:
                raise ValueError(f"{name}: count считает строки grain, measure не нужна")
        if self.additivity != _ADDITIVITY[self.kind]:
            raise ValueError(f"{name}: {self.kind} допускает только {_ADDITIVITY[self.kind]}")
        if self.unit not in _UNITS[self.kind]:
            raise ValueError(f"{name}: единица {self.kind} — одна из {_UNITS[self.kind]}")
        if self.unit == "money":
            if (self.currency is None) == (self.currency_column is None):
                raise ValueError(f"{name}: у денег нужна currency или currency_column")
        elif self.currency is not None or self.currency_column is not None:
            raise ValueError(f"{name}: валюта есть только у денежных метрик")
        for field in ("measure", "currency_column", "time_column"):
            ref = getattr(self, field)
            if ref is not None and ref.table != self.grain.table:
                raise ValueError(f"{name}.{field}: колонка не из таблицы grain")
        return self

    def validate_against(
        self, catalog: DatasetCatalog, metrics: Mapping[str, MetricDefinition] | None = None
    ) -> None:
        """Проверяет, что колонки, ключ grain и составляющие отношения есть в датасете.

        ``metrics`` — подтверждённые определения датасета по ``metric_id``; нужны отношению.
        Ошибка — ``ValueError`` с текстом для пользователя.
        """
        name = self.metric_id
        table = catalog.table(self.grain.table)
        if table is None:
            raise ValueError(f"{name}: нет таблицы {self.grain.table}")
        key = table.column(self.grain.key)
        if key is None or not key.is_unique:
            raise ValueError(f"{name}: {self.grain.table}.{self.grain.key} — не уникальный ключ")
        if self.measure is not None:
            column = _column(catalog, self.measure, name)
            if self.kind == "sum" and column.semantic_type not in NUMERIC:
                raise ValueError(f"{name}: сумма по нечисловой колонке {self.measure.column}")
        if self.time_column is not None:
            column = _column(catalog, self.time_column, name)
            if column.semantic_type not in TEMPORAL:
                raise ValueError(f"{name}: {self.time_column.column} — не дата")
        if self.currency_column is not None:
            _column(catalog, self.currency_column, name)
        for condition in self.filters:
            _column(catalog, condition.column, name)
            if condition.column.table != self.grain.table and (
                dimension_relation(catalog, self.grain.table, condition.column.table) is None
            ):
                raise ValueError(
                    f"{name}: фильтр по {condition.column.table} без связи N:1 от grain"
                )
        if self.kind == "ratio":
            for part in (self.numerator, self.denominator):
                other = (metrics or {}).get(part)
                if other is None or other.status != "confirmed":
                    raise ValueError(f"{name}: метрика {part} не подтверждена")
                if other.kind == "ratio":
                    raise ValueError(f"{name}: {part} — отношение, а нужна сумма или счёт")


class MetricDefinition(MetricSpec):
    """Сохранённая версия определения: в расчёт идёт только ``confirmed``."""

    version: int = Field(default=1, ge=1)
    status: MetricStatus = "proposed"
    confirmation: MetricConfirmation | None = None

    @model_validator(mode="after")
    def _status_matches_confirmation(self) -> MetricDefinition:
        if self.status == "confirmed" and self.confirmation is None:
            raise ValueError(f"{self.metric_id}: подтверждённое определение без confirmation")
        if self.status == "proposed" and self.confirmation is not None:
            raise ValueError(f"{self.metric_id}: предложенное определение ещё не подтверждено")
        return self


def _column(catalog: DatasetCatalog, ref: ColumnRef, name: str) -> Column:
    table = catalog.table(ref.table)
    column = table.column(ref.column) if table is not None else None
    if column is None:
        raise ValueError(f"{name}: нет колонки {ref.table}.{ref.column}")
    return column


def dimension_relation(catalog: DatasetCatalog, grain_table: str, table: str) -> Relation | None:
    """Связь ``grain_table → table`` вида N:1 или 1:1 с уверенностью ``high``.

    Связи ``medium``/``low`` (совпадение значений без совпадения имён и т. п.) не годятся:
    уверенность связи не утверждает смысл JOIN. Найденная связь — только кандидат:
    фактическое размножение и потери строк проверяются на данных при расчёте.
    """
    return next(
        (
            r
            for r in catalog.relations
            if r.from_.table == grain_table
            and r.to.table == table
            and r.cardinality in ("N:1", "1:1")
            and r.confidence == "high"
        ),
        None,
    )


class JoinCheck(_Model):
    """Проверка JOIN grain-таблицы с измерением на данных: размножение и потери строк."""

    from_: ColumnRef = Field(alias="from")
    to: ColumnRef
    rows_before: Count
    rows_after: Count
    max_fanout: Count
    null_key_rows: Count
    unmatched_rows: Count


class MetricVersionRef(_Model):
    metric_id: MetricId
    version: int = Field(ge=1)


class MetricProvenance(_Model):
    """Происхождение расчёта: версия определения и применённые ограничения."""

    metric_id: MetricId
    version: int = Field(ge=1)
    title: str
    formula: str  # человекочитаемая формула, например SUM(payments.amount)
    grain: MetricGrain
    additivity: Additivity
    unit: MetricUnit
    currency: str | None = None  # валюта результата; None — не денежная метрика
    filters: list[MetricFilter] = Field(default_factory=list)  # популяция и фильтры вопроса
    period_start: str | None = None  # ISO, включительно
    period_end: str | None = None  # ISO, не включительно
    group_by: list[ColumnRef] = Field(default_factory=list)
    components: list[MetricVersionRef] = Field(default_factory=list)  # числитель, знаменатель
    checks: list[JoinCheck] = Field(default_factory=list)


def metrics_prompt(metrics: Mapping[str, MetricDefinition]) -> str:
    """Раздел «Метрики» для агента: подтверждённые определения и явное «не определено»."""
    lines = ["Метрики (определения подтвердил пользователь; считать их только compute_metric):"]
    confirmed = [m for m in metrics.values() if m.status == "confirmed"]
    if not confirmed:
        lines.append("- подтверждённых определений нет.")
    for metric in sorted(confirmed, key=lambda m: m.metric_id):
        lines.append(f"- {metric.metric_id} v{metric.version} «{metric.title}»: {_formula(metric)}")
    lines.append(
        "Бизнес-метрики вне этого списка (выручка, средний чек, конверсия и т. п.) не "
        "определены: имя колонки, описание в каталоге и уверенность связи их не задают."
    )
    return "\n".join(lines)


def _formula(metric: MetricDefinition) -> str:
    if metric.kind == "ratio":
        parts = [f"{metric.numerator} / {metric.denominator}"]
    else:
        target = (
            metric.grain.table
            if metric.measure is None
            else f"{metric.measure.table}.{metric.measure.column}"
        )
        parts = [f"{metric.kind.upper()}({target})"]
        if metric.filters:
            parts.append(
                "где "
                + " и ".join(
                    f"{f.column.table}.{f.column.column} {f.op}"
                    + ("" if f.value is None else f" {f.value}")
                    for f in metric.filters
                )
            )
    parts.append(f"grain {metric.grain.table}.{metric.grain.key}")
    if metric.currency:
        parts.append(f"валюта {metric.currency}")
    if metric.currency_column:
        parts.append(f"валюта в {metric.currency_column.table}.{metric.currency_column.column}")
    if metric.time_column:
        parts.append(f"календарь {metric.time_column.table}.{metric.time_column.column}")
    parts.append(
        "суммируется по группам и периодам"
        if metric.additivity == "additive"
        else "не суммируется: пересчитывать по группам и периодам"
    )
    return "; ".join(parts)
