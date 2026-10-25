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
from sqlglot.optimizer.scope import Scope, traverse_scope

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
    if metric.currency_column is not None and request.currency is not None:
        currency_ref = metric.currency_column
        where.append(f"{column(currency_ref)} = {_literal(request.currency)}")
        grain_where.append(f"{_q(currency_ref.column)} = {_literal(request.currency)}")
        currency = request.currency

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

    source = f"{_q(grain)} AS {_GRAIN_ALIAS}"
    for table, join in joins.items():
        relation = join.relation
        # проверка выше гарантирует пару для каждой строки: LEFT JOIN не меняет число строк
        source += (
            f" LEFT JOIN {_q(table)} AS {join.alias} ON "
            f"{_GRAIN_ALIAS}.{_q(relation.from_.column)} = {join.alias}.{_q(relation.to.column)}"
        )
    condition = " WHERE " + " AND ".join(f"({p})" for p in where) if where else ""

    currency_ref = metric.currency_column
    if (
        currency_ref is not None
        and request.currency is None
        and currency_ref not in request.group_by
    ):
        # валюты всей расчётной популяции: с фильтрами измерений и периодом, как в расчёте
        codes = _currencies(connection, column(currency_ref), source + condition, allowed_tables)
        if len(codes) > 1:
            raise MetricError(
                "metric_mixed_currency",
                f"В «{metric.title}» смешаны валюты: {', '.join(codes)}. Суммы в разных "
                "валютах не складываются без курса.",
                f"Уточните валюту (currency: {', '.join(codes)}) или разбейте по "
                f"{currency_ref.table}.{currency_ref.column}.",
            )
        currency = codes[0] if codes else None

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
    sql = f"SELECT {', '.join(select)} FROM {source}{condition}"
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
    column: str,
    population: str,
    allowed_tables: Collection[str] | None,
) -> list[str]:
    """Коды валют в строках ``population`` (FROM … WHERE … расчёта)."""
    sql = f"SELECT DISTINCT CAST({column} AS VARCHAR) FROM {population} ORDER BY 1"
    rows = execute_dataset_query(connection, sql, allowed_tables=allowed_tables).fetchall()
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


# --- Защита run_sql: колонка подтверждённой метрики не агрегируется после fanout ---

_ORDER_INSENSITIVE = (exp.Min, exp.Max)  # повтор строки не меняет min/max


@dataclass
class _Source:
    alias: str
    table: str | None  # таблица каталога; None — подзапрос или CTE
    unique: set[str]  # колонки (casefold), уникальные в источнике
    scope: Scope | None = None


def metric_guard(query: exp.Expression, semantics: MetricContext) -> MetricError | None:
    """Ошибка, если агрегат колонки подтверждённой метрики идёт через JOIN с fanout/потерями.

    Строки источника с мерой сохраняются, только если от него по равенствам JOIN каждый
    следующий источник присоединён по уникальной колонке (и тогда строки не размножаются),
    а INNER JOIN не теряет строк: связь каталога покрывает все значения и ключ без пустых.
    Источник без такого пути (CROSS JOIN, условие не на равенство, неуникальный ключ)
    считается размножающим. ``COUNT(DISTINCT)`` и подзапросы без GROUP BY по ключу эту
    проверку не проходят: DISTINCT не восстанавливает grain.
    """
    measures = {
        (m.measure.table.casefold(), m.measure.column.casefold()): m
        for m in semantics.metrics.values()
        if m.status == "confirmed" and m.measure is not None
    }
    if not measures:
        return None
    catalog = semantics.catalog
    for scope in traverse_scope(query):
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        sources = _sources(scope, catalog)
        for function in select.find_all(exp.AggFunc):
            if isinstance(function, _ORDER_INSENSITIVE) or _owner(function) is not select:
                continue
            for column in function.find_all(exp.Column):
                source = _column_source(column, sources, catalog)
                if source is None:
                    continue
                metric, problem = _trace(source, column.name, measures, catalog)
                if metric is None:
                    continue
                # JOIN этого уровня или JOIN внутри CTE/подзапроса, отдающего строки меры
                problem = _fanout(select, sources, source, catalog) or problem
                if problem is not None:
                    code, detail = problem
                    return MetricError(
                        code,
                        f"Запрос агрегирует «{metric.title}» ({metric.measure.table}."
                        f"{metric.measure.column}) через JOIN, который {detail}: "
                        "результат был бы искажён.",
                        f"Используйте compute_metric с metric_id='{metric.metric_id}' или "
                        "агрегируйте меру до JOIN по ключу её grain.",
                    )
    return None


def _owner(node: exp.Expression) -> exp.Expression | None:
    return node.find_ancestor(exp.Select)


def _sources(scope: Scope, catalog: DatasetCatalog) -> dict[str, _Source]:
    sources = {}
    for alias, (_, source) in scope.selected_sources.items():
        key = alias.casefold()
        if isinstance(source, exp.Table):
            table = catalog.table(source.name) or next(
                (t for t in catalog.tables if t.table_id.casefold() == source.name.casefold()),
                None,
            )
            unique = {c.name.casefold() for c in table.columns if c.is_unique} if table else set()
            sources[key] = _Source(key, table.table_id if table else None, unique)
        elif isinstance(source, Scope):
            sources[key] = _Source(key, None, _derived_unique(source, catalog), source)
    return sources


def _derived_unique(scope: Scope, catalog: DatasetCatalog) -> set[str]:
    """Уникальные колонки подзапроса: ключи GROUP BY или ключи единственной таблицы."""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return set()
    projections = {
        p.alias_or_name.casefold(): p.unalias() for p in select.expressions if p.alias_or_name
    }
    group = select.args.get("group")
    if group is not None:
        keys = []
        for item in group.expressions:
            if isinstance(item, exp.Literal) and item.is_int:
                index = int(item.this) - 1
                if 0 <= index < len(select.expressions):
                    item = select.expressions[index].unalias()
            keys.append(item)
        names = {
            name
            for name, expression in projections.items()
            if any(expression == key for key in keys)
        }
        return names if len(keys) == 1 else set()
    inner = _sources(scope, catalog)
    if len(inner) != 1 or select.args.get("distinct"):
        return set()
    (source,) = inner.values()
    return {
        name
        for name, expression in projections.items()
        if isinstance(expression, exp.Column) and expression.name.casefold() in source.unique
    }


def _column_source(
    column: exp.Column, sources: dict[str, _Source], catalog: DatasetCatalog
) -> _Source | None:
    if column.table:
        return sources.get(column.table.casefold())
    owners = [s for s in sources.values() if column.name.casefold() in _columns_of(s, catalog)]
    return owners[0] if len(owners) == 1 else None


def _columns_of(source: _Source, catalog: DatasetCatalog) -> set[str]:
    if source.table is not None:
        return {c.name.casefold() for c in catalog.table(source.table).columns}
    if source.scope is None:
        return set()
    branches = _branches(source.scope)
    if not branches:
        return set()
    if renamed := _alias_columns(source.scope):
        return set(renamed)
    first = branches[0]
    names = {
        p.alias_or_name.casefold()
        for p in first.expression.expressions
        if not _is_star(p) and p.alias_or_name
    }
    inner = _sources(first, catalog)
    for projection in first.expression.expressions:
        if _is_star(projection):
            table = _star_table(projection)
            owners = [inner[table]] if table in inner else inner.values()
            for owner in owners:
                names |= _columns_of(owner, catalog)
    return names


def _branches(scope: Scope) -> list[Scope]:
    """SELECT-ветки подзапроса: сам SELECT или все ветки UNION/INTERSECT/EXCEPT."""
    if isinstance(scope.expression, exp.Select):
        return [scope]
    branches = []
    for branch in scope.set_operation_scopes or []:
        branches += _branches(branch)
    return branches


def _is_star(projection: exp.Expression) -> bool:
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
    )


def _alias_columns(scope: Scope) -> list[str]:
    """Имена колонок из алиаса ``WITH e(x, y)`` или ``(SELECT …) AS e(x, y)``."""
    alias = scope.expression.parent.args.get("alias") if scope.expression.parent else None
    columns = alias.columns if isinstance(alias, exp.TableAlias) else []
    return [c.name.casefold() for c in columns]


def _star_table(projection: exp.Expression) -> str | None:
    """Источник ``t.*`` (casefold); у голой ``*`` источника нет."""
    if isinstance(projection, exp.Column) and projection.table:
        return projection.table.casefold()
    return None


def _projections(scope: Scope, column: str) -> list[tuple[Scope, exp.Expression]]:
    """Выражения, из которых подзапрос отдаёт колонку ``column``, по всем веткам.

    Ветки UNION сопоставляются по позиции, как в SQL; ``*`` и ``t.*`` отдают колонку
    источника с тем же именем.
    """
    branches = _branches(scope)
    if not branches:
        return []
    named = _alias_columns(scope) or [
        None if _is_star(p) else p.alias_or_name.casefold()
        for p in branches[0].expression.expressions
    ]
    found = []
    for branch in branches:
        projections = branch.expression.expressions
        if column.casefold() in named:
            index = named.index(column.casefold())
            if index < len(projections):
                found.append((branch, projections[index].unalias()))
            continue
        for projection in projections:
            if _is_star(projection):
                found.append((branch, exp.column(column, table=_star_table(projection))))
    return found


def _trace(
    source: _Source,
    column: str,
    measures: Mapping[tuple[str, str], MetricDefinition],
    catalog: DatasetCatalog,
) -> tuple[MetricDefinition | None, tuple[str, str] | None]:
    """Метрика, мера которой попадает в ``source.column``, и fanout по пути к ней.

    Мера проходит через CTE и подзапросы. Уровень, который отдаёт строки меры без
    агрегата, проверяется своим JOIN-ом: размноженные там строки внешний SUM сложит.
    Уровень с агрегатом меры проверяет основной цикл ``metric_guard`` как отдельный SELECT.
    """
    if source.table is not None:
        return measures.get((source.table.casefold(), column.casefold())), None
    if source.scope is None:
        return None, None
    found: MetricDefinition | None = None
    for branch, expression in _projections(source.scope, column):
        inner = _sources(branch, catalog)
        aggregated = expression.find(exp.AggFunc) is not None
        for ref in expression.find_all(exp.Column):
            owner = _column_source(ref, inner, catalog)
            if owner is None:
                continue
            metric, problem = _trace(owner, ref.name, measures, catalog)
            if metric is None:
                continue
            found = found or metric
            if not aggregated:
                problem = _fanout(branch.expression, inner, owner, catalog) or problem
                if problem is not None:
                    return metric, problem
    return found, None


def _fanout(
    select: exp.Select,
    sources: dict[str, _Source],
    start: _Source,
    catalog: DatasetCatalog,
) -> tuple[str, str] | None:
    """Код и описание, если JOIN размножает или теряет строки ``start``; иначе ``None``."""
    order, sides, edges = _join_graph(select, sources)
    reached = {start.alias}
    pending = [start.alias]
    while pending:
        current = pending.pop()
        for (left, left_column), (right, right_column) in edges:
            for here, there, there_column in (
                (left, right, right_column),
                (right, left, left_column),
            ):
                if here == current and there not in reached:
                    if there_column.casefold() not in sources[there].unique:
                        return (
                            "metric_grain_violation",
                            f"размножает строки: {there}.{there_column} не уникален",
                        )
                    reached.add(there)
                    pending.append(there)
    missing = [alias for alias in sources if alias not in reached]
    if missing:
        return (
            "metric_grain_violation",
            f"присоединяет {', '.join(missing)} без равенства по уникальному ключу",
        )
    for index, alias in enumerate(order[1:], start=1):
        side = sides[alias]
        left_part = set(order[:index])
        if start.alias in left_part and side not in ("LEFT", "FULL"):
            if not _complete(edges, left_part, alias, sources, catalog):
                return "metric_rows_lost", f"теряет строки без пары в {alias}"
        if start.alias == alias and side not in ("RIGHT", "FULL"):
            if not _complete(edges, {alias}, None, sources, catalog, left_part):
                return "metric_rows_lost", f"теряет строки {alias} без пары"
    return None


def _join_graph(select: exp.Select, sources: dict[str, _Source]):
    """Порядок источников, сторона каждого JOIN и равенства колонок между источниками."""
    from_ = select.args.get("from_")
    order = [from_.this.alias_or_name.casefold()] if from_ is not None else []
    sides: dict[str, str] = {}
    conditions: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        alias = join.this.alias_or_name.casefold()
        order.append(alias)
        sides[alias] = "CROSS" if join.kind == "CROSS" else (join.side or "INNER").upper()
        if join.args.get("on") is not None:
            conditions.append(join.args["on"])
        for identifier in join.args.get("using") or []:
            name = identifier.name
            for previous in order[:-1]:
                conditions.append(
                    exp.EQ(
                        this=exp.column(name, table=previous),
                        expression=exp.column(name, table=alias),
                    )
                )
                break
    if select.args.get("where") is not None:
        conditions.append(select.args["where"].this)
    edges = []
    for condition in conditions:
        for term in condition.flatten() if isinstance(condition, exp.And) else [condition]:
            if not isinstance(term, exp.EQ):
                continue
            left, right = term.this, term.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            if not left.table or not right.table:
                continue
            a, b = left.table.casefold(), right.table.casefold()
            if a != b and a in sources and b in sources:
                edges.append(((a, left.name), (b, right.name)))
    return order, sides, edges


def _complete(
    edges,
    side: set[str],
    joined: str | None,
    sources: dict[str, _Source],
    catalog: DatasetCatalog,
    other: set[str] | None = None,
) -> bool:
    """Каждая строка ``side`` находит пару: связь каталога покрывает все значения ключа."""
    targets = {joined} if joined is not None else (other or set())
    for (a, a_column), (b, b_column) in edges:
        for here, here_column, there, there_column in (
            (a, a_column, b, b_column),
            (b, b_column, a, a_column),
        ):
            if here not in side or there not in targets:
                continue
            here_ref = _base_column(sources[here], here_column, catalog)
            there_ref = _base_column(sources[there], there_column, catalog)
            if here_ref is None or there_ref is None:
                continue
            (here_table, here_column), (there_table, there_column) = here_ref, there_ref
            column = catalog.table(here_table).column(here_column)
            relation = next(
                (
                    r
                    for r in catalog.relations
                    if (r.from_.table, r.from_.column, r.to.table, r.to.column)
                    == (here_table, here_column, there_table, there_column)
                ),
                None,
            )
            if (
                column is not None
                and column.null_ratio == 0
                and relation is not None
                and relation.overlap == 1.0
            ):
                return True
    return False


def _base_column(source: _Source, column: str, catalog: DatasetCatalog) -> tuple[str, str] | None:
    """Колонка таблицы каталога за ``source.column``: прямо или через простой подзапрос.

    Подзапрос из одной таблицы только отбирает её строки, поэтому полнота связи и пустые
    ключи исходной колонки к нему применимы.
    """
    if source.table is not None:
        table = catalog.table(source.table)
        found = next((c for c in table.columns if c.name.casefold() == column.casefold()), None)
        return (table.table_id, found.name) if found is not None else None
    if source.scope is None or not isinstance(source.scope.expression, exp.Select):
        return None
    inner = _sources(source.scope, catalog)
    if len(inner) != 1:
        return None
    for projection in source.scope.expression.expressions:
        expression = projection.unalias()
        if projection.alias_or_name.casefold() == column.casefold() and isinstance(
            expression, exp.Column
        ):
            (only,) = inner.values()
            return _base_column(only, expression.name, catalog)
    return None
