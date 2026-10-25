"""Потери и неоднозначности типизации в колонках конкретного расчёта (B2-10, #168).

``assess`` дополняет результат инструмента отчётом ``QualityReport``: какие колонки
каталога использовал его SQL (``djedai.analytics.lineage``), сколько их значений не
распознано именно в популяции расчёта и какие ограничения из журнала преобразований
к ним относятся.

Typed view превращает нераспознанное значение в NULL, и отличить его от исходного
пропуска можно только по сырой таблице. Поэтому популяция пересчитывается отдельным
запросом: копия scope расчёта (FROM, JOIN, WHERE; без GROUP BY и LIMIT), где typed view
заменён подзапросом над ``raw_<имя>`` с флагами «значение было» и «значение не
распознано». Флаги проходят через CTE и подзапросы до scope популяции. Строки без пары
в LEFT JOIN не считаются ни пропусками, ни потерями.

Если популяцию так не посчитать (коррелированный подзапрос, UNION внутри подзапроса,
ключ JOIN, ошибка запроса), берётся профиль всей таблицы из журнала, а ``boundary``
говорит, что это не число потерь в расчёте. Правила — в
``docs/decisions/168-typing-losses.md``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from djedai.analytics.lineage import ColumnUse, Lineage, trace
from djedai.analytics.queries import check_budget
from djedai.analytics.scope import parse_read_query
from djedai.config import get_limits
from djedai.contracts.catalog import Column, DatasetCatalog, Table
from djedai.contracts.quality import (
    ColumnQuality,
    QualityIssue,
    QualityReport,
    QualityScope,
    Severity,
    Usage,
)
from djedai.contracts.tool_result import Fact, ToolResult
from djedai.execution import query_timer

logger = logging.getLogger(__name__)

_PRESENT = "__djedai_present_"
_FAILED = "__djedai_failed_"
_RAW = "__djedai_raw_"
_DROP_CLAUSES = ("group", "having", "order", "limit", "offset", "distinct", "qualify", "windows")
_COMPUTED: frozenset[Usage] = frozenset({"measure", "filter", "group"})
_TYPE_NAMES = {
    "integer": "целое число",
    "float": "число",
    "money": "деньги",
    "percent": "процент",
    "date": "дата",
    "datetime": "дата и время",
    "bool": "да/нет",
    "number": "число",
}

BOUNDARY_PROFILE = "посчитано по всей таблице: сколько из этих значений попало в расчёт, неизвестно"


@dataclass(frozen=True)
class _Counts:
    rows: int
    source_empty: int
    non_empty: int
    failed: int
    scope: QualityScope
    boundary: str | None = None


@dataclass
class _Column:
    table: Table
    column: Column
    action: str  # parse_* или keep_text
    detail: dict
    examples: list[str]
    uses: list[ColumnUse] = field(default_factory=list)
    counts: _Counts | None = None  # популяция мер, вывода и группировки
    dropped: list[tuple[int, _Counts]] = field(default_factory=list)  # фильтры: строки и база
    mixed_with: set[tuple[str, str | None]] = field(default_factory=set)  # другие валюты
    mixed_in_measure: bool = False

    @property
    def usages(self) -> set[Usage]:
        return {use.usage for use in self.uses}

    @property
    def name(self) -> str:
        return f"«{self.column.original_name}» ({self.table.table_id}.{self.column.name})"


def assess(
    result: ToolResult, connection: duckdb.DuckDBPyConnection, catalog: DatasetCatalog
) -> ToolResult:
    """Результат с отчётом о качестве колонок его SQL и фактами для ответа."""
    if result.status == "error" or not result.sql:
        return result
    try:
        lineage = trace(parse_read_query(result.sql), catalog)
    except (ValueError, sqlglot.errors.SqlglotError) as error:
        lineage = Lineage(None, unresolved=[f"запрос не разобран: {error}"])
    report = quality_report(lineage, connection, catalog)
    facts = [*result.facts, *quality_facts(report)]
    return result.model_copy(update={"quality": report, "facts": facts})


def quality_report(
    lineage: Lineage, connection: duckdb.DuckDBPyConnection, catalog: DatasetCatalog
) -> QualityReport:
    columns: dict[tuple[str, str], _Column] = {}
    checked: list[str] = []
    uses = list(lineage.uses)
    if lineage.query is None:
        # колонки не связать с запросом: проверяются все колонки упомянутых таблиц
        for table_id in sorted(lineage.tables):
            table = catalog.table(table_id)
            for column in table.columns:
                uses.append(ColumnUse(table_id, column.name, "output", None, 0, 0, column.name, ()))
    for use in uses:
        checked.append(f"{use.table}.{use.column}")
        key = (use.table, use.column)
        if key not in columns:
            column = _column(catalog, use.table, use.column)
            if column is None:
                continue
            columns[key] = column
        columns[key].uses.append(use)
    if lineage.query is not None:
        _count_populations(lineage, [c for c in columns.values() if _needs_count(c)], connection)
    for column in columns.values():
        if column.counts is None:
            column.counts = _profile(column, BOUNDARY_PROFILE)
    _mixed_currency_expressions(columns.values())
    reported = []
    for column in columns.values():
        issues = _issues(column)
        if issues:
            reported.append(_quality(column, issues))
    return QualityReport(
        lineage="partial" if lineage.unresolved else "complete",
        unresolved=lineage.unresolved,
        checked=list(dict.fromkeys(checked)),
        columns=reported,
    )


def quality_facts(report: QualityReport) -> list[Fact]:
    facts = []
    if report.lineage == "partial":
        facts.append(
            Fact(
                text="Не все колонки расчёта удалось связать с данными: ограничения ниже "
                "могут быть неполными",
                kind="data_quality",
                values={"lineage": "partial"},
            )
        )
    for column in report.columns:
        for issue in column.issues:
            facts.append(
                Fact(
                    text=issue.message,
                    kind="data_quality",
                    values={
                        "code": issue.code,
                        "severity": issue.severity,
                        "table": column.table,
                        "column": column.column,
                        "scope": column.scope,
                        "rows": column.rows,
                        "source_empty": column.source_empty,
                        "non_empty": column.non_empty,
                        "parse_failed": column.parse_failed,
                        "failed_ratio": column.failed_ratio,
                    },
                )
            )
    return facts


# --- колонки и журнал ---


def _column(catalog: DatasetCatalog, table_id: str, name: str) -> _Column | None:
    table = catalog.table(table_id)
    if table is None or table.raw_table is None:
        return None  # живой источник: типы задала БД, разбора не было
    column = table.column(name)
    if column is None:
        return None
    entries = [t for t in catalog.transformations if t.table == table_id and t.column == name]
    decision = next(
        (e for e in entries if e.action.startswith("parse_") or e.action == "keep_text"), None
    )
    unparsed = next((e for e in entries if e.action == "unparsed_values"), None)
    return _Column(
        table,
        column,
        decision.action if decision else "keep_text",
        dict(decision.detail) if decision else {},
        [str(v) for v in unparsed.detail.get("examples", [])] if unparsed else [],
    )


def _needs_count(column: _Column) -> bool:
    detail = column.detail
    return column.action.startswith("parse_") and detail.get("parsed", 0) < detail.get(
        "non_empty", 0
    )


def _profile(column: _Column, boundary: str) -> _Counts:
    rows = column.table.row_count
    non_empty = column.detail.get("non_empty")
    if non_empty is None:
        source_empty = round(column.column.null_ratio * rows)
        return _Counts(rows, source_empty, rows - source_empty, 0, "table", boundary)
    parsed = column.detail.get("parsed", non_empty)
    return _Counts(rows, rows - non_empty, non_empty, non_empty - parsed, "table", boundary)


# --- популяция расчёта ---


def _count_populations(
    lineage: Lineage, columns: list[_Column], connection: duckdb.DuckDBPyConnection
) -> None:
    jobs: dict[tuple[int, ColumnUse | None], list[tuple[_Column, ColumnUse]]] = defaultdict(list)
    for column in columns:
        for use in column.uses:
            if use.boundary is not None or use.usage == "join_key":
                continue
            jobs[(use.target, use if use.usage == "filter" else None)].append((column, use))
    for (target, filter_use), batch in jobs.items():
        try:
            sql = _population_sql(lineage, target, batch, filter_use, connection)
            row = _fetch(connection, sql)
        except (duckdb.Error, sqlglot.errors.SqlglotError, KeyError, ValueError) as error:
            check_budget()
            logger.warning("Популяцию для проверки качества посчитать не удалось: %s", error)
            continue
        for index, (column, _use) in enumerate(batch):
            rows, non_empty, failed, dropped = row[index * 4 : index * 4 + 4]
            counts = _Counts(rows, rows - non_empty, non_empty, failed, "population")
            if filter_use is not None:
                column.dropped.append((dropped, counts))
            elif column.counts is None or counts.failed > column.counts.failed:
                # одна колонка в нескольких популяциях: показываем худшую
                column.counts = counts
    for column in columns:
        if column.counts is None and column.dropped and column.usages <= {"filter", "join_key"}:
            # только фильтр: популяция — строки до этого фильтра
            column.counts = max(column.dropped, key=lambda item: item[0])[1]
        for use in column.uses:
            if column.counts is None and use.boundary is not None:
                column.counts = _profile(column, f"{use.boundary}; {BOUNDARY_PROFILE}")


def _fetch(connection: duckdb.DuckDBPyConnection, sql: str) -> tuple:
    check_budget()
    timer = query_timer(get_limits().tool_timeout_seconds, connection.interrupt)
    timer.start()
    try:
        return connection.execute(sql).fetchone()
    finally:
        timer.cancel()


def _population_sql(
    lineage: Lineage,
    target: int,
    batch: list[tuple[_Column, ColumnUse]],
    filter_use: ColumnUse | None,
    connection: duckdb.DuckDBPyConnection,
) -> str:
    tree = lineage.query.copy()
    scopes = list(traverse_scope(tree))
    if len(scopes) != len(lineage.scopes):
        raise ValueError("копия запроса разобрана иначе")
    replaced: set[tuple[int, str]] = set()
    flags: list[tuple[str, str, str]] = []  # алиас в scope популяции, флаг «было», «не распознано»
    for number, (column, use) in enumerate(batch):
        entry = use.path[-1]
        if (entry.scope, entry.alias) not in replaced:
            node = scopes[entry.scope].sources[entry.alias]
            node.replace(_quality_relation(connection, column.table, entry.alias))
            replaced.add((entry.scope, entry.alias))
        alias, present, failed = entry.alias, _PRESENT + use.column, _FAILED + use.column
        for inner, outer in zip(reversed(use.path[1:]), reversed(use.path[:-1]), strict=True):
            select = scopes[inner.scope].expression
            names = (f"__djedai_p{number}", f"__djedai_f{number}")
            select.select(
                *(
                    exp.alias_(_ref(alias, flag), name, quoted=True)
                    for flag, name in zip((present, failed), names, strict=True)
                ),
                copy=False,
            )
            alias, (present, failed) = outer.alias, names
        flags.append((alias, present, failed))

    counted = scopes[target].expression.copy()
    for clause in _DROP_CLAUSES:
        counted.set(clause, None)
    condition: exp.Expression = exp.false()
    if filter_use is not None:
        condition = _split_filter(counted, filter_use)
    projections = []
    for alias, present, failed in flags:
        p, f = _ref(alias, present), _ref(alias, failed)
        projections += [
            _count_if(exp.Not(this=exp.Is(this=p, expression=exp.null()))),
            _count_if(p),
            _count_if(f),
            _count_if(exp.and_(f, exp.Not(this=_coalesce_false(condition)))),
        ]
    counted.set("expressions", projections)
    with_ = tree.args.get("with")
    if with_ is not None and counted.args.get("with") is None:
        counted.set("with", with_.copy())
    return counted.sql("duckdb")


def _split_filter(select: exp.Select, use: ColumnUse) -> exp.Expression:
    """Убирает из WHERE условия на колонку и возвращает их: строки, которые они отсекли."""
    where = select.args.get("where")
    if where is None:
        raise ValueError("фильтр не в WHERE")
    alias = use.path[0].alias
    conjuncts = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
    removed = [
        c
        for c in conjuncts
        if any(col.table == alias and col.name == use.ref for col in c.find_all(exp.Column))
    ]
    if not removed:
        raise ValueError("фильтр не в WHERE")
    kept = [c for c in conjuncts if all(c is not r for r in removed)]
    select.set("where", exp.Where(this=exp.and_(*kept)) if kept else None)
    return exp.and_(*(c.copy() for c in removed))


def _quality_relation(
    connection: duckdb.DuckDBPyConnection, table: Table, alias: str
) -> exp.Expression:
    """Подзапрос над сырой таблицей: колонки typed view и флаги «было» и «не распознано»."""
    row = connection.execute(
        "SELECT sql FROM duckdb_views() WHERE view_name = ? AND database_name = current_database()",
        [table.table_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"представление {table.table_id} не найдено")
    view = sqlglot.parse_one(row[0], read="duckdb").expression.copy()
    for index, column in enumerate(table.columns):
        view.select(
            exp.alias_(
                exp.column(column.original_name, quoted=True), f"{_RAW}{index}", quoted=True
            ),
            copy=False,
        )
    flagged = exp.select("*").from_(view.subquery("t"))
    for index, column in enumerate(table.columns):
        raw = exp.column(f"{_RAW}{index}", table="t", quoted=True)
        present = _coalesce_false(
            exp.NEQ(this=exp.Trim(this=raw), expression=exp.Literal.string(""))
        )
        typed = exp.column(column.name, table="t", quoted=True)
        flagged.select(
            exp.alias_(present, _PRESENT + column.name, quoted=True),
            exp.alias_(
                exp.and_(present.copy(), exp.Is(this=typed, expression=exp.null())),
                _FAILED + column.name,
                quoted=True,
            ),
            copy=False,
        )
    return flagged.subquery(alias)


def _coalesce_false(condition: exp.Expression) -> exp.Expression:
    return exp.Coalesce(this=condition, expressions=[exp.false()])


def _ref(alias: str, name: str) -> exp.Column:
    return exp.column(name, table=alias, quoted=True)


def _count_if(condition: exp.Expression) -> exp.Expression:
    return exp.Filter(this=exp.Count(this=exp.Star()), expression=exp.Where(this=condition))


# --- правила ---


def _mixed_currency_expressions(columns: Iterable[_Column]) -> None:
    """Отмечает денежные колонки разных валют, которые складываются в одном выражении."""
    by_expression: dict[int, list[tuple[_Column, ColumnUse]]] = defaultdict(list)
    for column in columns:
        if column.column.semantic_type == "money":
            for use in column.uses:
                if use.usage in ("measure", "output"):
                    by_expression[use.expression].append((column, use))
    for members in by_expression.values():
        currencies = {c.column.currency for c, _ in members}
        if len(currencies) > 1:
            for column, use in members:
                column.mixed_with.update(
                    (other.name, other.column.currency)
                    for other, _ in members
                    if other.column.currency != column.column.currency
                )
                column.mixed_in_measure |= use.usage == "measure"


def _issues(column: _Column) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    usages, detail, name = column.usages, column.detail, column.name
    counts = column.counts
    computed = bool(usages & _COMPUTED)

    def add(code, severity: Severity, message: str) -> None:
        issues.append(QualityIssue(code=code, severity=severity, message=message))

    if column.action.startswith("parse_"):
        target = _TYPE_NAMES.get(column.column.semantic_type, column.column.semantic_type)
        if counts.failed and usages - {"filter", "join_key"}:
            examples = (
                f" Примеры нераспознанных значений в таблице: {', '.join(column.examples)}."
                if column.examples
                else ""
            )
            text = (
                f"{name}: {counts.failed} из {counts.non_empty} непустых значений "
                f"({_percent(counts.failed, counts.non_empty)}) не распознаны как {target} "
                f"и не вошли в расчёт."
            )
            if counts.scope == "table":
                text = (
                    f"{name}: в таблице {counts.failed} из {counts.non_empty} непустых значений "
                    f"({_percent(counts.failed, counts.non_empty)}) не распознаны как {target}; "
                    f"{counts.boundary}."
                )
            add("parse_losses", "warning", text + examples)
        for dropped, base in column.dropped:
            if dropped:
                add(
                    "parse_losses",
                    "warning",
                    f"{name}: фильтр по этой колонке отбросил строки, где исходное значение "
                    f"есть, но не распознано: {dropped} из {base.non_empty} непустых.",
                )
    if column.action == "keep_text":
        reason = detail.get("reason")
        if reason == "below_threshold" and computed:
            threshold = round(get_limits().type_parse_threshold * 100)
            candidate = _TYPE_NAMES.get(detail.get("candidate"), detail.get("candidate"))
            add(
                "kept_as_text",
                "warning",
                f"{name} осталась текстом: как {candidate} распознано "
                f"{_percent_ratio(detail.get('success_ratio', 0))} значений, ниже порога "
                f"{threshold}%. Типизированного расчёта по ней нет.",
            )
        elif reason == "ambiguous_date" and usages:
            formats = ", ".join(detail.get("candidates", []))
            add(
                "ambiguous_date",
                "blocking" if computed else "warning",
                f"{name}: день и месяц не различить (подходят форматы {formats}), даты "
                f"остались текстом. Нужно уточнить формат.",
            )
        elif reason == "mixed_currencies" and computed:
            currencies = ", ".join(detail.get("currencies", []))
            add(
                "mixed_currency",
                "blocking" if "measure" in usages else "warning",
                f"{name}: в колонке несколько валют ({currencies}), значения остались текстом "
                f"и не складываются. Нужно выбрать валюту или считать по каждой отдельно.",
            )
    if detail.get("ambiguous_values"):
        alternatives = ", ".join(detail.get("alternative_formats", []))
        add(
            "ambiguous_date",
            "warning",
            f"{name}: формат {detail.get('format')} выбран по большинству значений, но "
            f"{detail['ambiguous_values']} из {detail.get('non_empty', '?')} подходят и под "
            f"{alternatives}: день и месяц в них могли поменяться местами.",
        )
    if "ambiguous_separator" in str(detail.get("note", "")) and usages:
        add(
            "ambiguous_separator",
            "warning",
            f"{name}: одиночный разделитель («1,234») принят за десятичный; если это "
            f"разделитель тысяч, значения в 1000 раз меньше.",
        )
    if detail.get("without_percent_sign") and usages:
        add(
            "percent_scale",
            "warning",
            f"{name}: {detail['without_percent_sign']} значений записаны без знака % "
            f"(например, 0,15 рядом с 15%): их масштаб неизвестен, они взяты как есть.",
        )
    if column.column.semantic_type == "percent" and any(
        use.aggregate == "sum" for use in column.uses
    ):
        add(
            "percent_sum",
            "warning",
            f"{name}: проценты сложены; сумма процентов не является долей.",
        )
    if column.mixed_with:
        others = ", ".join(f"{other} в {currency}" for other, currency in sorted(column.mixed_with))
        add(
            "mixed_currency",
            "blocking" if column.mixed_in_measure else "warning",
            f"{name} в {column.column.currency} складывается в одном выражении с {others}: "
            f"курсов нет, такая сумма не имеет смысла.",
        )
    return issues


def _quality(column: _Column, issues: list[QualityIssue]) -> ColumnQuality:
    counts = column.counts
    return ColumnQuality(
        table=column.table.table_id,
        column=column.column.name,
        raw_table=column.table.raw_table,
        original_name=column.column.original_name,
        transformation=column.action,
        target_type=column.column.semantic_type if column.action.startswith("parse_") else None,
        format=column.column.parse.format if column.column.parse else None,
        usage=sorted(column.usages),
        scope=counts.scope,
        boundary=counts.boundary,
        rows=counts.rows,
        source_empty=counts.source_empty,
        non_empty=counts.non_empty,
        parse_failed=counts.failed,
        failed_ratio=round(counts.failed / counts.non_empty, 4) if counts.non_empty else 0.0,
        examples=column.examples if counts.failed else [],
        issues=issues,
    )


def _percent(part: int, total: int) -> str:
    return _percent_ratio(part / total if total else 0.0)


def _percent_ratio(ratio: float) -> str:
    text = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"
