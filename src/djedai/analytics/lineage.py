"""Lineage SQL-запроса до колонок каталога (B2-10, #168).

Запрос квалифицируется схемой каталога (``sqlglot.optimizer.qualify``): у каждой колонки
появляется источник, ``*`` раскрывается, ``GROUP BY 1`` заменяется выражением. Затем для
каждой ссылки на колонку определяется, как она используется (``measure`` — внутри
агрегата, ``filter``, ``join_key``, ``group``, ``output``), и путь через CTE и подзапросы
до таблицы каталога.

Популяция использования (``target``) — scope, на строках которого колонка агрегируется
или выводится: колонка из CTE без агрегации проходит наружу, и её строки — это строки
внешнего запроса после его JOIN и WHERE. Агрегат внутри CTE, GROUP BY или DISTINCT
останавливают проход: популяция — строки этого CTE.

Всё, что не удаётся связать с колонкой каталога, попадает в ``unresolved``: lineage
неполная, и это показывается пользователю, а не угадывается.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.quality import Usage

_CLAUSE_USAGE: dict[str, Usage] = {
    "where": "filter",
    "qualify": "filter",
    "having": "filter",
    "joins": "join_key",
    "group": "group",
}


@dataclass(frozen=True)
class Hop:
    """Шаг пути: scope и имя источника в нём, через который идёт колонка."""

    scope: int  # индекс в ``Lineage.scopes``
    alias: str


@dataclass(frozen=True)
class ColumnUse:
    table: str  # table_id каталога
    column: str
    usage: Usage
    aggregate: str | None  # sum, avg, count, … — если колонка внутри агрегата
    expression: int  # ключ выражения: колонки одного выражения складываются вместе
    target: int  # scope популяции
    ref: str  # имя колонки в scope популяции (после переименований в CTE)
    path: tuple[Hop, ...]  # от target до таблицы; последний шаг — алиас таблицы
    boundary: str | None = None  # почему точное число в популяции не посчитать
    # условие FILTER агрегата в scope популяции (SQL): мера считается только на его строках,
    # а колонка из самого условия — фильтр (``usage="filter"``)
    condition: str | None = None


@dataclass
class Lineage:
    query: exp.Expression | None  # квалифицированный запрос; None — квалификация не удалась
    scopes: list[Scope] = field(default_factory=list)
    uses: list[ColumnUse] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)  # таблицы каталога, упомянутые в запросе


def trace(query: exp.Expression, catalog: DatasetCatalog) -> Lineage:
    """Колонки каталога, на которых основан результат ``query``, и их популяции."""
    tables = _catalog_tables(query, catalog)
    schema = {t.table_id: {c.name: c.physical_type for c in t.columns} for t in catalog.tables}
    try:
        qualified = qualify(query.copy(), schema=schema, dialect="duckdb")
        scopes = list(traverse_scope(qualified))
    except (OptimizeError, SqlglotError) as error:
        return Lineage(None, unresolved=[f"запрос не разобран: {error}"], tables=tables)
    tracer = _Tracer(catalog, scopes)
    for index, scope in enumerate(scopes):
        if isinstance(scope.expression, exp.Select):
            tracer.scan(index, scope)
    uses = list(dict.fromkeys(tracer.uses))
    return Lineage(qualified, scopes, uses, list(dict.fromkeys(tracer.unresolved)), tables)


def _catalog_tables(query: exp.Expression, catalog: DatasetCatalog) -> set[str]:
    names = {t.table_id.casefold(): t.table_id for t in catalog.tables}
    return {
        names[t.name.casefold()] for t in query.find_all(exp.Table) if t.name.casefold() in names
    }


class _Tracer:
    def __init__(self, catalog: DatasetCatalog, scopes: list[Scope]) -> None:
        self.catalog = catalog
        self.scopes = scopes
        self.index = {id(scope): i for i, scope in enumerate(scopes)}
        self.uses: list[ColumnUse] = []
        self.unresolved: list[str] = []

    def scan(self, index: int, scope: Scope) -> None:
        select = scope.expression
        # Колонки, выводимые CTE и подзапросом, разбирает их потребитель: только он знает,
        # агрегируются ли они и на каких строках. Ветви UNION верхнего уровня — как корень.
        passthrough = scope.scope_type in (ScopeType.CTE, ScopeType.DERIVED_TABLE) or (
            scope.scope_type == ScopeType.SET_OPERATION and not _root_branch(scope)
        )
        boundary = (
            "коррелированный подзапрос: строки зависят от внешней строки"
            if scope.is_correlated_subquery
            else None
        )
        for column, clause in _own_columns(select):
            if clause == "order":
                continue
            aggregate = _aggregate(column, select)
            condition, in_condition = _aggregate_filter(column, select)
            if clause == "expressions":
                if passthrough:
                    continue
                usage: Usage = "measure" if aggregate else _window_usage(column, select)
            else:
                usage = "measure" if clause == "having" and aggregate else _CLAUSE_USAGE[clause]
            if in_condition:
                usage = "filter"
            expression = id(_clause_node(column, select))
            self._resolve(
                index, scope, column.table, column.name, usage, aggregate, expression,
                target=index, ref=column.name, path=(), boundary=boundary, condition=condition,
            )  # fmt: skip

    def _resolve(
        self,
        index: int,
        scope: Scope,
        alias: str,
        name: str,
        usage: Usage,
        aggregate: str | None,
        expression: int,
        *,
        target: int,
        ref: str,
        path: tuple[Hop, ...],
        boundary: str | None,
        condition: str | None = None,
    ) -> None:
        source = scope.sources.get(alias)
        if source is None:
            # колонка внешнего запроса в коррелированном подзапросе
            outer = scope.parent
            while outer is not None and alias not in outer.sources:
                outer = outer.parent
            if outer is None:
                self.unresolved.append(f"{alias}.{name}: источник не найден")
                return
            self._resolve(
                self.index[id(outer)], outer, alias, name, usage, aggregate, expression,
                target=self.index[id(outer)], ref=name, path=(),
                boundary="коррелированный подзапрос: строки зависят от внешней строки",
            )  # fmt: skip
            return
        path = (*path, Hop(index, alias))
        if isinstance(source, exp.Table):
            table = self.catalog.table(source.name) or next(
                (t for t in self.catalog.tables if t.table_id.casefold() == source.name.casefold()),
                None,
            )
            if table is None:
                self.unresolved.append(f"{source.name}.{name}: таблицы нет в каталоге")
                return
            column = next((c for c in table.columns if c.name.casefold() == name.casefold()), None)
            if column is None:
                self.unresolved.append(f"{table.table_id}.{name}: колонки нет в каталоге")
                return
            self.uses.append(
                ColumnUse(
                    table.table_id,
                    column.name,
                    usage,
                    aggregate,
                    expression,
                    target,
                    ref,
                    path,
                    boundary,
                    condition,
                )
            )
            return
        if not isinstance(source, Scope):
            self.unresolved.append(f"{alias}.{name}: источник не таблица и не подзапрос")
            return
        inner_index = self.index.get(id(source))
        inner = source.expression
        if inner_index is None:
            self.unresolved.append(f"{alias}.{name}: подзапрос не разобран")
            return
        if isinstance(inner, exp.SetOperation):
            # UNION внутри подзапроса: колонка ветви считается на строках ветви
            branches = list(_branches(source))
            names = [p.alias_or_name.casefold() for p in branches[0].expression.expressions]
            if name.casefold() not in names:
                self.unresolved.append(f"{alias}.{name}: колонки нет в UNION")
                return
            position = names.index(name.casefold())
            for branch in branches:
                select = branch.expression
                for column in _columns_in(select.expressions[position], select):
                    branch_condition, in_condition = _aggregate_filter(column, select)
                    self._resolve(
                        self.index[id(branch)], branch, column.table, column.name,
                        "filter" if in_condition else usage,
                        _aggregate(column, select) or aggregate, expression,
                        target=self.index[id(branch)], ref=column.name, path=(),
                        boundary="UNION внутри подзапроса: потери посчитаны по ветке",
                        condition=branch_condition,
                    )  # fmt: skip
            return
        if not isinstance(inner, exp.Select):
            self.unresolved.append(f"{alias}.{name}: подзапрос не SELECT")
            return
        projection = next(
            (p for p in inner.expressions if p.alias_or_name.casefold() == name.casefold()), None
        )
        if projection is None:
            self.unresolved.append(f"{alias}.{name}: колонки нет в подзапросе")
            return
        stops = bool(inner.args.get("group") or inner.args.get("distinct") or _has_aggregate(inner))
        for column in _columns_in(projection, inner):
            inner_aggregate = _aggregate(column, inner)
            inner_condition, in_condition = _aggregate_filter(column, inner)
            if stops or inner_aggregate or in_condition:
                # агрегат, GROUP BY или DISTINCT в подзапросе: популяция — его строки
                inner_usage: Usage = "measure" if inner_aggregate else "group"
                if usage == "measure" and not inner_aggregate:
                    inner_usage = "measure"
                if in_condition:
                    inner_usage = "filter"
                self._resolve(
                    inner_index, source, column.table, column.name, inner_usage,
                    inner_aggregate or aggregate, id(projection), target=inner_index,
                    ref=column.name, path=(), boundary=boundary, condition=inner_condition,
                )  # fmt: skip
            else:
                self._resolve(
                    inner_index, source, column.table, column.name, usage, aggregate,
                    expression, target=target, ref=ref, path=path, boundary=boundary,
                    condition=condition,
                )  # fmt: skip


def _branches(scope: Scope) -> Iterator[Scope]:
    for child in scope.set_operation_scopes:
        if isinstance(child.expression, exp.SetOperation):
            yield from _branches(child)
        else:
            yield child


def _root_branch(scope: Scope) -> bool:
    """Ветвь UNION верхнего уровня: её строки и есть результат запроса."""
    parent = scope.parent
    while parent is not None and isinstance(parent.expression, exp.SetOperation):
        if parent.scope_type == ScopeType.ROOT:
            return True
        parent = parent.parent
    return False


def _own_columns(select: exp.Select) -> Iterator[tuple[exp.Column, str]]:
    """Колонки этого SELECT (без вложенных подзапросов) и раздел, где они стоят."""
    for key, value in select.args.items():
        if key == "with" or value is None:
            continue
        for node in value if isinstance(value, list) else [value]:
            if not isinstance(node, exp.Expression):
                continue
            for column in _columns_in(node, select):
                yield column, key


def _columns_in(node: exp.Expression, select: exp.Select) -> Iterator[exp.Column]:
    for column in node.find_all(exp.Column):
        if column.table and column.find_ancestor(exp.Select) is select:
            yield column


def _clause_node(column: exp.Column, select: exp.Select) -> exp.Expression:
    node: exp.Expression = column
    while node.parent is not None and node.parent is not select:
        node = node.parent
    return node


def _aggregate(column: exp.Column, select: exp.Select) -> str | None:
    node = column.parent
    while node is not None and node is not select:
        if isinstance(node, exp.AggFunc):
            return node.key
        node = node.parent
    return None


def _aggregate_filter(column: exp.Column, select: exp.Select) -> tuple[str | None, bool]:
    """Условие ``FILTER (WHERE …)`` агрегата вокруг колонки и стоит ли она в самом условии."""
    node: exp.Expression = column
    while node.parent is not None and node is not select:
        parent = node.parent
        if isinstance(parent, exp.Filter):
            return parent.expression.this.sql("duckdb"), node is parent.expression
        node = parent
    return None, False


def _has_aggregate(select: exp.Select) -> bool:
    return any(
        function.find_ancestor(exp.Select) is select and not isinstance(function.parent, exp.Window)
        for projection in select.expressions
        for function in projection.find_all(exp.AggFunc)
    )


def _window_usage(column: exp.Column, select: exp.Select) -> Usage:
    window = column.find_ancestor(exp.Window)
    if window is not None and window.find_ancestor(exp.Select) is select:
        if any(
            column is c
            for p in window.args.get("partition_by") or []
            for c in p.find_all(exp.Column)
        ):
            return "group"
    return "output"
