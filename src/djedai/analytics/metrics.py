"""Расчёт подтверждённых бизнес-метрик на их grain (B2-9, #166).

Метрика агрегируется по строкам своей grain-таблицы. Колонки измерений для группировки и
фильтров присоединяются только по связи каталога N:1 с уверенностью ``high``
(``dimension_relation``), и перед расчётом JOIN проверяется на данных расчётной популяции
(``measure_join_impact``): больше одной строки измерения на ключ — ``metric_grain_violation``,
пустые ключи или строки без пары — ``metric_rows_lost``. ``DISTINCT`` размножение не
исправляет и поэтому не применяется; ``COUNT(DISTINCT)`` есть только у метрик вида
``count_distinct``, где он и есть определение.

Деньги в нескольких валютах не складываются: без выбранной валюты или группировки по
колонке валюты расчёт отказывает с ``metric_mixed_currency``. Отношение считается из
числителя и знаменателя, каждый на своём grain и со своей популяцией, и делится после
агрегации — не суммой и не средним долей.

SQL собирается из определения, идентификаторы экранируются, значения — литералы sqlglot.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import duckdb
from pydantic import ValidationError
from sqlglot import exp

from djedai.analytics.queries import execute_dataset_query
from djedai.config import get_limits
from djedai.contracts.catalog import ColumnRef, DatasetCatalog, Relation
from djedai.contracts.metrics import (
    JoinCheck,
    MetricDefinition,
    MetricFilter,
    MetricProvenance,
    MetricVersionRef,
    dimension_relation,
)
from djedai.contracts.tool_result import ChartHint, Fact, ToolError, ToolResult, ToolResultData
from djedai.data.relations import measure_join_impact
from djedai.execution import query_timer

TIME_GRAINS = ("day", "week", "month", "quarter", "year")
_OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_GRAIN_ALIAS = "g"
PERIOD_COLUMN = "period"
CURRENCY_COLUMN = "currency"
VALUE_COLUMN = "value"


@dataclass(frozen=True)
class MetricContext:
    """Каталог и подтверждённые определения датасета для ``compute_metric`` и ``run_sql``."""

    catalog: DatasetCatalog
    metrics: Mapping[str, MetricDefinition] = field(default_factory=dict)


class MetricError(Exception):
    """Расчёт отклонён; ``code`` и тексты идут в ``ToolError``."""

    def __init__(self, code: str, message: str, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


@dataclass(frozen=True)
class MetricRequest:
    """Срез, который запросил агент, поверх популяции из определения."""

    group_by: tuple[ColumnRef, ...] = ()
    filters: tuple[MetricFilter, ...] = ()
    period_start: str | None = None  # ISO, включительно
    period_end: str | None = None  # ISO, не включительно
    time_grain: str | None = None
    currency: str | None = None  # выбранная валюта для метрик с колонкой валюты


@dataclass(frozen=True)
class CompiledMetric:
    sql: str
    output: tuple[str, ...]  # имена колонок результата
    provenance: MetricProvenance


def compile_metric(
    connection: duckdb.DuckDBPyConnection,
    context: MetricContext,
    metric_id: str,
    request: MetricRequest,
    *,
    allowed_tables: Collection[str] | None = None,
) -> CompiledMetric:
    """Собирает SQL метрики и проверяет grain JOIN-ов на данных; ошибка — ``MetricError``."""
    metric = context.metrics.get(metric_id)
    if metric is None or metric.status != "confirmed":
        defined = ", ".join(sorted(context.metrics)) or "нет"
        raise MetricError(
            "metric_undefined",
            f"Метрика «{metric_id}» не определена.",
            "Уточните у пользователя определение: grain, формулу, популяцию, валюту и "
            f"период. Подтверждённые метрики: {defined}.",
        )
    try:
        metric.validate_against(context.catalog, context.metrics)
    except ValueError as error:
        raise MetricError(
            "metric_definition_stale",
            f"Определение «{metric.title}» не соответствует данным: {error}",
            "Попросите пользователя обновить определение метрики.",
        ) from None
    _validate_request(request)
    if metric.kind != "ratio":
        part = _compile_part(connection, context, metric, request, allowed_tables)
        return CompiledMetric(part.sql, part.output, part.provenance)

    numerator = _compile_part(
        connection, context, context.metrics[metric.numerator], request, allowed_tables
    )
    denominator = _compile_part(
        connection, context, context.metrics[metric.denominator], request, allowed_tables
    )
    keys = numerator.output[:-1]
    currency = _ratio_currency(metric, numerator.provenance, denominator.provenance)
    select = [f"coalesce(n.{_q(k)}, d.{_q(k)}) AS {_q(k)}" for k in keys]
    select += [
        f"n.{_q(VALUE_COLUMN)} AS numerator",
        f"d.{_q(VALUE_COLUMN)} AS denominator",
        # знаменатель 0 или без строк — значения нет, а не бесконечность
        f"n.{_q(VALUE_COLUMN)} / NULLIF(d.{_q(VALUE_COLUMN)}, 0) AS {_q(VALUE_COLUMN)}",
    ]
    join = " AND ".join(f"n.{_q(k)} IS NOT DISTINCT FROM d.{_q(k)}" for k in keys) or "TRUE"
    order = f" ORDER BY {', '.join(str(i + 1) for i in range(len(keys)))}" if keys else ""
    sql = (
        f"WITH n AS ({numerator.sql}), d AS ({denominator.sql}) "
        f"SELECT {', '.join(select)} FROM n FULL OUTER JOIN d ON {join}{order}"
    )
    provenance = MetricProvenance(
        metric_id=metric.metric_id,
        version=metric.version,
        title=metric.title,
        formula=f"({numerator.provenance.formula}) / ({denominator.provenance.formula})",
        grain=metric.grain,
        additivity=metric.additivity,
        unit=metric.unit,
        currency=currency,
        filters=[*numerator.provenance.filters, *denominator.provenance.filters],
        period_start=request.period_start,
        period_end=request.period_end,
        group_by=list(request.group_by),
        components=[
            MetricVersionRef(metric_id=p.metric_id, version=p.version)
            for p in (numerator.provenance, denominator.provenance)
        ],
        checks=[*numerator.provenance.checks, *denominator.provenance.checks],
    )
    return CompiledMetric(sql, (*keys, "numerator", "denominator", VALUE_COLUMN), provenance)


def _validate_request(request: MetricRequest) -> None:
    start = _instant(request.period_start, "period_start")
    end = _instant(request.period_end, "period_end")
    if start is not None and end is not None and start >= end:
        raise MetricError(
            "invalid_parameter",
            "Начало периода должно быть раньше конца.",
            "Период полуоткрытый: [period_start, period_end).",
        )
    if request.time_grain is not None and request.time_grain not in TIME_GRAINS:
        raise MetricError(
            "invalid_parameter",
            f"Недопустимый шаг календаря: '{request.time_grain}'.",
            f"Допустимо: {', '.join(TIME_GRAINS)}.",
        )


def _instant(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MetricError(
            "invalid_parameter",
            f"{name}: ожидается дата ISO, например 2023-01-01.",
        ) from None
    if parsed.tzinfo is not None:
        raise MetricError(
            "invalid_parameter",
            f"{name}: время в данных хранится без часового пояса, укажите его без смещения.",
        )
    return parsed


@dataclass(frozen=True)
class _Join:
    alias: str
    relation: Relation


def _compile_part(
    connection: duckdb.DuckDBPyConnection,
    context: MetricContext,
    metric: MetricDefinition,
    request: MetricRequest,
    allowed_tables: Collection[str] | None,
) -> CompiledMetric:
    """SQL одной суммы или счёта: агрегат по строкам grain с измерениями N:1."""
    catalog = context.catalog
    grain = metric.grain.table
    filters = [*metric.filters, *request.filters]
    joins: dict[str, _Join] = {}
    for ref in [*request.group_by, *(f.column for f in filters)]:
        _require_column(catalog, ref)
        if ref.table == grain or ref.table in joins:
            continue
        relation = dimension_relation(catalog, grain, ref.table)
        if relation is None:
            raise MetricError(
                "metric_grain_violation",
                f"«{metric.title}» считается по строкам {grain}; {ref.table} не связана с ней "
                "подтверждённой связью N:1, и JOIN может размножить или потерять строки.",
                f"Сгруппируйте по колонкам {grain} или таблиц, на которые она ссылается.",
            )
        joins[ref.table] = _Join(f"d{len(joins) + 1}", relation)

    def column(ref: ColumnRef) -> str:
        alias = _GRAIN_ALIAS if ref.table == grain else joins[ref.table].alias
        return f"{alias}.{_q(ref.column)}"

    where = [_predicate(column(f.column), f) for f in filters]
    grain_where = [_predicate(_q(f.column.column), f) for f in filters if f.column.table == grain]
    if request.period_start is not None or request.period_end is not None:
        if metric.time_column is None:
            raise MetricError(
                "metric_period_unsupported",
                f"У «{metric.title}» нет колонки календаря: период к ней не применить.",
                "Уточните, за какой период считать эту метрику, или уберите период.",
            )
        for bound, op in ((request.period_start, ">="), (request.period_end, "<")):
            if bound is not None:
                literal = _timestamp(bound)
                where.append(f"{column(metric.time_column)} {op} {literal}")
                grain_where.append(f"{_q(metric.time_column.column)} {op} {literal}")

    currency = metric.currency
    if metric.currency_column is not None:
        currency_ref = metric.currency_column
        if request.currency is not None:
            where.append(f"{column(currency_ref)} = {_literal(request.currency)}")
            grain_where.append(f"{_q(currency_ref.column)} = {_literal(request.currency)}")
            currency = request.currency
        elif currency_ref not in request.group_by:
            codes = _currencies(connection, grain, currency_ref.column, grain_where, allowed_tables)
            if len(codes) > 1:
                raise MetricError(
                    "metric_mixed_currency",
                    f"В «{metric.title}» смешаны валюты: {', '.join(codes)}. Суммы в разных "
                    "валютах не складываются без курса.",
                    f"Уточните валюту (currency: {', '.join(codes)}) или разбейте по "
                    f"{currency_ref.table}.{currency_ref.column}.",
                )
            currency = codes[0] if codes else None

    checks = []
    for table, join in joins.items():
        relation = join.relation
        impact = measure_join_impact(
            connection,
            grain,
            relation.from_.column,
            table,
            relation.to.column,
            where=" AND ".join(f"({p})" for p in grain_where) or None,
        )
        check = JoinCheck(
            from_=relation.from_,
            to=relation.to,
            rows_before=impact.rows_before,
            rows_after=impact.rows_after,
            max_fanout=impact.max_fanout,
            null_key_rows=impact.null_key_rows,
            unmatched_rows=impact.unmatched_rows,
        )
        link = f"{relation.from_.table}.{relation.from_.column} → {table}.{relation.to.column}"
        if impact.max_fanout > 1:
            raise MetricError(
                "metric_grain_violation",
                f"JOIN {link} размножает строки {grain}: {impact.rows_before} → "
                f"{impact.rows_after} (до {impact.max_fanout} на ключ), «{metric.title}» "
                "была бы завышена.",
                "Уберите группировку или фильтр по этой таблице либо уточните определение.",
            )
        if impact.null_key_rows or impact.unmatched_rows:
            raise MetricError(
                "metric_rows_lost",
                f"JOIN {link} теряет строки «{metric.title}»: с пустым ключом "
                f"{impact.null_key_rows}, без пары {impact.unmatched_rows}.",
                "Уточните, как учитывать эти строки, или считайте без этой таблицы.",
            )
        checks.append(check)

    keys: list[tuple[str, str]] = []
    used: set[str] = set()
    for ref in request.group_by:
        name = ref.column if ref.column not in used else f"{ref.table}.{ref.column}"
        used.add(name)
        keys.append((name, column(ref)))
    if metric.currency_column is not None and metric.currency_column in request.group_by:
        currency = None  # у каждой строки своя валюта, она в колонке группировки
    if request.time_grain is not None:
        if metric.time_column is None:
            raise MetricError(
                "metric_period_unsupported",
                f"У «{metric.title}» нет колонки календаря для разбивки по периодам.",
            )
        bucket = f"CAST(date_trunc('{request.time_grain}', {column(metric.time_column)}) AS DATE)"
        keys.append((PERIOD_COLUMN, bucket))

    aggregate, formula = _aggregate(metric, column)
    select = [f"{expression} AS {_q(name)}" for name, expression in keys]
    select.append(f"{aggregate} AS {_q(VALUE_COLUMN)}")
    sql = f"SELECT {', '.join(select)} FROM {_q(grain)} AS {_GRAIN_ALIAS}"
    for table, join in joins.items():
        relation = join.relation
        # проверка выше гарантирует пару для каждой строки: LEFT JOIN не меняет число строк
        sql += (
            f" LEFT JOIN {_q(table)} AS {join.alias} ON "
            f"{_GRAIN_ALIAS}.{_q(relation.from_.column)} = {join.alias}.{_q(relation.to.column)}"
        )
    if where:
        sql += " WHERE " + " AND ".join(f"({p})" for p in where)
    if keys:
        positions = ", ".join(str(i + 1) for i in range(len(keys)))
        sql += f" GROUP BY {positions} ORDER BY {positions}"
    if filters:
        formula += " WHERE " + " AND ".join(_filter_text(f) for f in filters)
    provenance = MetricProvenance(
        metric_id=metric.metric_id,
        version=metric.version,
        title=metric.title,
        formula=formula,
        grain=metric.grain,
        additivity=metric.additivity,
        unit=metric.unit,
        currency=currency,
        filters=filters,
        period_start=request.period_start,
        period_end=request.period_end,
        group_by=list(request.group_by),
        checks=checks,
    )
    return CompiledMetric(sql, (*(name for name, _ in keys), VALUE_COLUMN), provenance)


def _aggregate(metric: MetricDefinition, column) -> tuple[str, str]:
    if metric.kind == "count":
        return "count(*)", f"COUNT({metric.grain.table})"
    ref = metric.measure
    if metric.kind == "count_distinct":
        return f"count(DISTINCT {column(ref)})", f"COUNT(DISTINCT {ref.table}.{ref.column})"
    return f"sum({column(ref)})", f"SUM({ref.table}.{ref.column})"


def _ratio_currency(
    metric: MetricDefinition, numerator: MetricProvenance, denominator: MetricProvenance
) -> str | None:
    """Деньги на штуку — в валюте числителя; деньги к деньгам — только в одной валюте."""
    if numerator.unit == "money" and denominator.unit == "money":
        if numerator.currency is None or numerator.currency != denominator.currency:
            raise MetricError(
                "metric_mixed_currency",
                f"Числитель и знаменатель «{metric.title}» в разных валютах.",
                "Уточните валюту расчёта.",
            )
        return None
    return numerator.currency if numerator.unit == "money" else None


def _currencies(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    where: Sequence[str],
    allowed_tables: Collection[str] | None,
) -> list[str]:
    condition = " AND ".join(f"({p})" for p in where)
    sql = f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) FROM {_q(table)}"
    if condition:
        sql += f" WHERE {condition}"
    rows = execute_dataset_query(
        connection, sql + " ORDER BY 1", allowed_tables=allowed_tables
    ).fetchall()
    # пустая валюта — тоже неизвестная валюта, а не «та же самая»
    return [row[0] if row[0] is not None else "не указана" for row in rows]


def _require_column(catalog: DatasetCatalog, ref: ColumnRef) -> None:
    table = catalog.table(ref.table)
    if table is None or table.column(ref.column) is None:
        raise MetricError(
            "invalid_column",
            f"Колонка {ref.table}.{ref.column} не найдена.",
            "Используйте таблицы и колонки из каталога.",
        )


def _predicate(column: str, condition: MetricFilter) -> str:
    if condition.op == "is_null":
        return f"{column} IS NULL"
    if condition.op == "not_null":
        return f"{column} IS NOT NULL"
    if condition.op in ("in", "not_in"):
        values = ", ".join(_literal(v) for v in condition.value)
        return f"{column} {'IN' if condition.op == 'in' else 'NOT IN'} ({values})"
    return f"{column} {_OPS[condition.op]} {_literal(condition.value)}"


def _filter_text(condition: MetricFilter) -> str:
    ref = f"{condition.column.table}.{condition.column.column}"
    return _predicate(ref, condition)


def _literal(value: object) -> str:
    return exp.convert(value).sql(dialect="duckdb")


def _timestamp(value: str) -> str:
    """Граница периода; дата без времени — полночь этого дня."""
    return f"TIMESTAMP {_literal(datetime.fromisoformat(value).isoformat(sep=' '))}"


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def compute_metric(
    metric_id: str,
    group_by: Sequence[str] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    time_grain: str | None = None,
    currency: str | None = None,
    filters: Sequence[Mapping[str, object]] | None = None,
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
    semantics: MetricContext | None = None,
) -> ToolResult:
    """Считает подтверждённую метрику; ``group_by`` — колонки вида ``таблица.колонка``."""
    started = time.perf_counter()
    params = {
        "metric_id": metric_id,
        "group_by": list(group_by or []),
        "period_start": period_start,
        "period_end": period_end,
        "time_grain": time_grain,
        "currency": currency,
        "filters": [dict(f) for f in filters or []],
    }
    if semantics is None or connection is None:
        return _error(
            params,
            MetricError(
                "metric_undefined",
                f"Метрика «{metric_id}» не определена: для датасета нет подтверждённых метрик.",
                "Уточните у пользователя определение метрики.",
            ),
        )
    limits = get_limits()
    timer = query_timer(limits.tool_timeout_seconds, connection.interrupt)
    timer.start()
    try:
        request = MetricRequest(
            group_by=tuple(_column_ref(value) for value in group_by or []),
            filters=tuple(_metric_filter(value) for value in filters or []),
            period_start=period_start,
            period_end=period_end,
            time_grain=time_grain,
            currency=currency,
        )
        compiled = compile_metric(
            connection, semantics, metric_id, request, allowed_tables=allowed_tables
        )
        cursor = execute_dataset_query(connection, compiled.sql, allowed_tables=allowed_tables)
        rows = [[_json_value(v) for v in row] for row in cursor.fetchmany(limits.sql_max_rows + 1)]
    except MetricError as error:
        return _error(params, error)
    except duckdb.Error as error:
        return _error(
            params,
            MetricError(
                "database_error",
                f"Ошибка базы данных: {error}",
                "Запрос выполнялся слишком долго или данные недоступны.",
            ),
        )
    finally:
        timer.cancel()
    truncated = len(rows) > limits.sql_max_rows
    rows = rows[: limits.sql_max_rows]
    provenance = compiled.provenance
    facts = [_definition_fact(provenance)]
    if len(rows) == 1 and len(compiled.output) == len(rows[0]):
        value = rows[0][-1]
        facts.append(
            Fact(
                text=f"{provenance.title}: {_value_text(value, provenance.currency)}",
                kind="metric",
                values={"value": value},
            )
        )
    keys = [c for c in compiled.output if c not in ("numerator", "denominator", VALUE_COLUMN)]
    return ToolResult(
        result_id=f"r_{uuid.uuid4().hex}",
        status="ok" if rows else "empty",
        tool="compute_metric",
        params=params,
        sql=compiled.sql,
        data=ToolResultData(
            columns=list(compiled.output), rows=rows, row_count=len(rows), truncated=truncated
        ),
        facts=facts,
        chart_hint=ChartHint(
            type="line" if PERIOD_COLUMN in keys else "bar", x=keys[0], y=[VALUE_COLUMN]
        )
        if keys
        else None,
        duration_ms=int((time.perf_counter() - started) * 1000),
        metric=provenance,
    )


def _error(params: dict, error: MetricError) -> ToolResult:
    return ToolResult(
        result_id=f"r_{uuid.uuid4().hex}",
        status="error",
        tool="compute_metric",
        params=params,
        error=ToolError(code=error.code, message=error.message, suggestion=error.suggestion),
    )


def _column_ref(value: str) -> ColumnRef:
    table, dot, column = str(value).partition(".")
    if not dot or not table or not column:
        raise MetricError(
            "invalid_parameter",
            f"group_by: '{value}' — нужна колонка вида таблица.колонка.",
        )
    return ColumnRef(table=table, column=column)


def _metric_filter(value: Mapping[str, object]) -> MetricFilter:
    data = dict(value)
    if isinstance(data.get("column"), str):
        data["column"] = _column_ref(data["column"])
    try:
        return MetricFilter.model_validate(data)
    except ValidationError as error:
        raise MetricError(
            "invalid_parameter",
            f"Некорректный фильтр {value}: {error.errors()[0]['msg']}",
            "Фильтр: {column: 'таблица.колонка', op: eq|ne|in|not_in|gt|gte|lt|lte|"
            "is_null|not_null, value}.",
        ) from None


def _definition_fact(provenance: MetricProvenance) -> Fact:
    """Что именно посчитано: версия определения и применённые ограничения."""
    parts = [f"«{provenance.title}» v{provenance.version}: {provenance.formula}"]
    parts.append(f"по строкам {provenance.grain.table}")
    if provenance.currency:
        parts.append(f"валюта {provenance.currency}")
    if provenance.period_start or provenance.period_end:
        parts.append(f"период [{provenance.period_start or '…'}, {provenance.period_end or '…'})")
    if provenance.components:
        parts.append(
            "составляющие "
            + ", ".join(f"{c.metric_id} v{c.version}" for c in provenance.components)
        )
    if provenance.additivity == "recompute":
        parts.append("значения групп и периодов не суммируются, а пересчитываются")
    return Fact(
        text="; ".join(parts),
        kind="metric_definition",
        values={"metric_id": provenance.metric_id, "version": provenance.version},
    )


def _value_text(value: object, currency: str | None) -> str:
    if value is None:
        return "нет значения"
    return f"{value} {currency}" if currency else str(value)


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
