"""Связи таблиц по пересечению ID (B1-5).

Кандидаты — колонки типа ``id`` и целые/текстовые колонки с ID-подобным именем или из
кандидатов в первичный ключ. Для пар кандидатов из разных таблиц значения сравниваются
как строки (``trim``, пустые не учитываются), на различных значениях:

- ``overlap = |A∩B| / |A|`` — доля значений ``from``, найденных в ``to``. Связь
  ориентирована от ссылки к ключу: ``to`` — уникальная сторона, поэтому кратность
  ``N:1`` или ``1:1``, ``1:N`` не выдаётся. Если не уникальна ни одна сторона — ``N:M``
  и ``overlap`` по меньшему множеству;
- ``jaccard = |A∩B| / |A∪B|``.

Похожие значения ещё не связь: два плотных диапазона ``1..50`` и ``1..4168`` пересекаются
полностью по меньшему множеству. Поэтому связь без совпадения имён колонок принимается
только при почти полном покрытии и высоком Jaccard, а ``N:M`` — только с совпадением имён.

Каждая связь проверяется JOIN-ом (``join_impact``): число строк ``from`` до и после
``LEFT JOIN``, строки без пары и суммы числовых колонок ``from``. Размножение строк и
строки без пары понижают ``confidence`` и дают предупреждения для каталога.

Связи сохраняются в ``_djedai_relations``; имена полей совпадают с ``Relation`` из
``DatasetCatalog`` (спецификация, §3.3–3.4).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from itertools import combinations

import duckdb

from djedai.data.profiler import (
    NUMERIC,
    ColumnProfile,
    TableProfile,
    _json_value,
    _wide_type,
    read_profiles,
)
from djedai.data.typed_views import _ID_NAME, _ident

RELATIONS_TABLE = "_djedai_relations"
MIN_OVERLAP = 0.5  # порог связи при совпадении имён; в спецификации помечен [TBD]
STRONG_OVERLAP = 0.95  # почти полное покрытие: high при совпадении имён, минимум без него
MEDIUM_OVERLAP = 0.8
MIN_JACCARD = 0.5  # без совпадения имён множества значений должны быть близки
MIN_DISTINCT = 2  # колонка из одного значения не ключ
KEY_TYPES = ("integer", "text")  # кроме ``id``: такие колонки — кандидаты по имени или ключу
_ID_TOKENS = ("id", "kod")


class RelationError(Exception):
    """Связи не удалось посчитать; ``str(error)`` можно показывать пользователю."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Связи таблиц: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class SumChange:  # сумма числовой колонки ``from`` до и после JOIN (JSON-значения)
    column: str
    before: object
    after: object


@dataclass(frozen=True)
class JoinImpact:  # что делает ``from LEFT JOIN to`` с таблицей ``from``
    rows_before: int
    rows_after: int
    unmatched_rows: int  # непустой ключ ``from`` без пары в ``to``
    max_fanout: int  # наибольшее число строк ``to`` на одно значение ключа
    sums: tuple[SumChange, ...]

    @property
    def multiplies(self) -> bool:
        return self.rows_after > self.rows_before

    @property
    def changed_sums(self) -> tuple[SumChange, ...]:
        return tuple(s for s in self.sums if s.before != s.after)


@dataclass(frozen=True)
class TableRelation:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    overlap: float
    jaccard: float
    cardinality: str  # 1:1 | N:1 | N:M
    confidence: str  # high | medium | low
    intersection: int  # |A∩B| различных значений
    from_distinct: int
    to_distinct: int
    name_match: bool
    impact: JoinImpact


@dataclass(frozen=True)
class RelationReport:
    relations: tuple[TableRelation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Key:  # колонка-кандидат: различные значения и уникальность по строковому ключу
    table: TableProfile
    column: ColumnProfile
    distinct: int
    unique: bool


def find_relations(connection: duckdb.DuckDBPyConnection) -> RelationReport:
    """Ищет связи по сохранённым профилям и записывает их; повторный вызов заменяет записи."""
    try:
        _ensure_table(connection)
        keys = _keys(connection, read_profiles(connection))
        relations = []
        for a, b in combinations(keys, 2):
            if a.table.view_name != b.table.view_name:
                relation = _relation(connection, a, b)
                if relation is not None:
                    relations.append(relation)
        relations.sort(key=lambda r: (r.from_table, r.from_column, r.to_table, r.to_column))
        connection.begin()
        try:
            _write_relations(connection, relations)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except duckdb.Error as error:
        raise RelationError(f"не удалось посчитать ({error})") from error
    return _report(relations)


def read_relations(connection: duckdb.DuckDBPyConnection) -> RelationReport:
    """Читает сохранённые связи; предупреждения строятся заново по проверке JOIN."""
    _ensure_table(connection)
    rows = connection.execute(
        f"SELECT relation FROM {RELATIONS_TABLE} "
        "ORDER BY from_table, from_column, to_table, to_column"
    ).fetchall()
    return _report([_relation_from_json(json.loads(row[0])) for row in rows])


def join_impact(
    connection: duckdb.DuckDBPyConnection,
    from_table: TableProfile,
    from_column: str,
    to_table: str,
    to_column: str,
) -> JoinImpact:
    """Число строк и суммы числовых колонок ``from`` до и после ``LEFT JOIN`` с ``to``.

    JOIN не материализуется: каждая строка ``from`` весит столько, сколько строк ``to``
    с её ключом (1 — если пары нет). Суммы считаются в точном типе, как в профайлере.
    """
    measures = [c for c in from_table.columns if c.semantic_type in NUMERIC]
    sums = []
    for column in measures:
        wide = _wide_type(column.physical_type, column.semantic_type)
        value = f"CAST({_ident(column.name)} AS {wide})" if wide else _ident(column.name)
        sums += [f"sum({value})", f"sum({value} * coalesce(m.n, 1))"]
    row = connection.execute(
        f"""WITH m AS (
                SELECT {_key(to_column)} AS k, count(*) AS n FROM {_ident(to_table)}
                WHERE {_key(to_column)} IS NOT NULL GROUP BY k),
            f AS (SELECT *, {_key(from_column)} AS k FROM {_ident(from_table.view_name)})
            SELECT count(*), coalesce(sum(coalesce(m.n, 1)), 0),
                count(*) FILTER (WHERE f.k IS NOT NULL AND m.n IS NULL),
                coalesce(max(m.n), 0){"".join(", " + s for s in sums)}
            FROM f LEFT JOIN m ON f.k = m.k"""
    ).fetchone()
    rows_before, rows_after, unmatched, fanout, *totals = row
    return JoinImpact(
        rows_before=rows_before,
        rows_after=int(rows_after),
        unmatched_rows=unmatched,
        max_fanout=fanout,
        sums=tuple(
            SumChange(c.name, _json_value(totals[2 * i]), _json_value(totals[2 * i + 1]))
            for i, c in enumerate(measures)
        ),
    )


def relation_warnings(relation: TableRelation) -> tuple[str, ...]:
    """Предупреждения каталога: размножение строк, изменение сумм, строки без пары."""
    impact = relation.impact
    link = (
        f"{relation.from_table}.{relation.from_column} → {relation.to_table}.{relation.to_column}"
    )
    warnings = []
    if impact.multiplies:
        text = (
            f"JOIN {link} размножает строки {relation.from_table}: "
            f"{impact.rows_before} → {impact.rows_after} (до {impact.max_fanout} на ключ)"
        )
        if impact.changed_sums:
            text += "; суммы меняются: " + ", ".join(
                f"{s.column} {s.before} → {s.after}" for s in impact.changed_sums
            )
        warnings.append(text)
    if impact.unmatched_rows:
        share = impact.unmatched_rows / impact.rows_before
        warnings.append(
            f"JOIN {link}: {impact.unmatched_rows} строк {relation.from_table} ({share:.1%}) "
            f"без пары, при INNER JOIN они пропадут"
        )
    return tuple(warnings)


def _report(relations: list[TableRelation]) -> RelationReport:
    return RelationReport(
        relations=tuple(relations),
        warnings=tuple(w for r in relations for w in relation_warnings(r)),
    )


def _key(column: str) -> str:
    """Значение для сравнения: строка без пробелов по краям, пустая — NULL."""
    return f"NULLIF(trim(CAST({_ident(column)} AS VARCHAR)), '')"


def _is_candidate(table: TableProfile, column: ColumnProfile) -> bool:
    if column.semantic_type == "id":
        return True
    return column.semantic_type in KEY_TYPES and (
        bool(_ID_NAME.search(column.name)) or column.name in table.primary_key_candidates
    )


def _keys(connection: duckdb.DuckDBPyConnection, profiles: list[TableProfile]) -> list[_Key]:
    keys = []
    for table in profiles:
        for column in table.columns:
            if not _is_candidate(table, column):
                continue
            present, distinct = connection.execute(
                f"SELECT count({_key(column.name)}), count(DISTINCT {_key(column.name)}) "
                f"FROM {_ident(table.view_name)}"
            ).fetchone()
            if distinct >= MIN_DISTINCT:
                keys.append(_Key(table, column, distinct, unique=present == distinct))
    return keys


def _intersection(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> int:
    def values(key: _Key) -> str:
        return f"SELECT DISTINCT {_key(key.column.name)} AS k FROM {_ident(key.table.view_name)}"

    return connection.execute(
        f"SELECT count(*) FROM ({values(a)}) a JOIN ({values(b)}) b ON a.k = b.k"
    ).fetchone()[0]


def _orient(a: _Key, b: _Key) -> tuple[_Key, _Key, str]:
    """``(from, to, кратность)``: ``to`` — уникальная сторона (ключ)."""
    if a.unique != b.unique:
        return (b, a, "N:1") if a.unique else (a, b, "N:1")
    if a.unique:  # 1:1 — ссылается меньшее множество; при равенстве ключ таблицы
        first, second = sorted((a, b), key=_one_to_one_order)
        return first, second, "1:1"
    first, second = sorted((a, b), key=lambda k: (k.table.view_name, k.column.name))
    return first, second, "N:M"


def _one_to_one_order(key: _Key) -> tuple:
    keys = key.table.primary_key_candidates
    is_first_key = bool(keys) and keys[0] == key.column.name
    return (key.distinct, is_first_key, key.table.view_name, key.column.name)


def _stem(name: str) -> str:
    return "_".join(token for token in name.split("_") if token not in _ID_TOKENS)


def _table_stems(table: str) -> set[str]:
    stems = {table}
    if table.endswith("ies"):
        stems.add(table[:-3] + "y")
    if table.endswith("s"):
        stems.add(table[:-1])
    return stems


def _name_match(source: _Key, target: _Key) -> bool:
    """Имена колонок говорят об одной сущности: ``store_id`` ↔ ``store_id``,
    ``client_id`` → ``clients.id``."""
    left, right = _stem(source.column.name), _stem(target.column.name)
    if left and left == right:
        return True
    if not right and left:
        return left in _table_stems(target.table.view_name)
    if not left and right:
        return right in _table_stems(source.table.view_name)
    return False


def _relation(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> TableRelation | None:
    common = _intersection(connection, a, b)
    if not common:
        return None
    source, target, cardinality = _orient(a, b)
    base = source.distinct if cardinality != "N:M" else min(a.distinct, b.distinct)
    overlap = common / base
    jaccard = common / (a.distinct + b.distinct - common)
    named = _name_match(source, target)
    if named:
        accepted = overlap >= MIN_OVERLAP
    else:  # похожие значения без общего имени — связь только при почти полном совпадении
        accepted = cardinality != "N:M" and overlap >= STRONG_OVERLAP and jaccard >= MIN_JACCARD
    if not accepted:
        return None
    impact = join_impact(
        connection, source.table, source.column.name, target.table.view_name, target.column.name
    )
    return TableRelation(
        from_table=source.table.view_name,
        from_column=source.column.name,
        to_table=target.table.view_name,
        to_column=target.column.name,
        overlap=round(overlap, 4),
        jaccard=round(jaccard, 4),
        cardinality=cardinality,
        confidence=_confidence(overlap, named, cardinality, impact),
        intersection=common,
        from_distinct=source.distinct,
        to_distinct=target.distinct,
        name_match=named,
        impact=impact,
    )


def _confidence(overlap: float, named: bool, cardinality: str, impact: JoinImpact) -> str:
    if not named or cardinality == "N:M" or impact.multiplies:
        return "low"
    if overlap >= STRONG_OVERLAP and not impact.unmatched_rows:
        return "high"
    return "medium" if overlap >= MEDIUM_OVERLAP else "low"


def _ensure_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {RELATIONS_TABLE} (
            from_table VARCHAR, from_column VARCHAR, to_table VARCHAR, to_column VARCHAR,
            relation VARCHAR, found_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (from_table, from_column, to_table, to_column))"""
    )


def _write_relations(connection: duckdb.DuckDBPyConnection, relations: list[TableRelation]) -> None:
    connection.execute(f"DELETE FROM {RELATIONS_TABLE}")
    for relation in relations:
        connection.execute(
            f"INSERT INTO {RELATIONS_TABLE} (from_table, from_column, to_table, to_column, "
            "relation) VALUES (?, ?, ?, ?, ?)",
            [
                relation.from_table,
                relation.from_column,
                relation.to_table,
                relation.to_column,
                json.dumps(asdict(relation), ensure_ascii=False),
            ],
        )


def _relation_from_json(data: dict) -> TableRelation:
    impact = data["impact"]
    return TableRelation(
        **{
            **data,
            "impact": JoinImpact(
                **{**impact, "sums": tuple(SumChange(**item) for item in impact["sums"])}
            ),
        }
    )
