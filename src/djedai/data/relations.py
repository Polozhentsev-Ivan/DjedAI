"""Связи таблиц по пересечению ID (B1-5).

Кандидаты — колонки типа ``id`` и целые/текстовые колонки с ID-подобным именем или из
кандидатов в первичный ключ. Для пар кандидатов из разных таблиц значения сравниваются
как строки, точно так же, как их сравнит ``JOIN ... ON a.x = b.y`` (без ``trim`` и других
скрытых нормализаций; NULL не учитывается), на различных значениях:

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

Сравниваются только ключи одного семейства типов: текст с текстом, целые с целыми.
``JOIN ON число = текст`` приводит текст к числу (``'01'`` совпадает с ``1``), и строковое
сравнение скрыло бы размножение строк и изменение сумм. Такие пары не дают связей, а если
похожи на связь по именам или значениям, о них есть предупреждение.

Если ключи совпадают только после удаления пробелов по краям (``'1 '`` и ``'1'``),
связь по точным значениям не находится или слабее, и в предупреждениях сказано, что
JOIN по этим колонкам без ``trim`` теряет строки.

Связи сохраняются в ``_djedai_relations``; ``TableRelation.to_contract()`` даёт ``Relation``
для ``DatasetCatalog.relations``, а ``RelationReport.warnings`` идут в ``warnings`` каталога
(спецификация, §3.3–3.4).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from itertools import combinations
from pathlib import Path

import duckdb

from djedai.contracts.catalog import ColumnRef, Relation
from djedai.data.csv_ingest import read_sources, slugify
from djedai.data.postgres import INTEGER_TYPES, read_db_sources
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
WARNINGS_TABLE = "_djedai_relation_warnings"
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
    after: object  # после LEFT JOIN
    inner_after: object  # после INNER JOIN: строки без пары и с пустым ключом пропадают


@dataclass(frozen=True)
class JoinImpact:  # что делает JOIN ``from`` с ``to`` с таблицей ``from``
    rows_before: int
    rows_after: int  # после LEFT JOIN
    rows_inner: int  # после INNER JOIN
    unmatched_rows: int  # непустой ключ ``from`` без пары в ``to``
    null_key_rows: int  # пустой ключ ``from``: пары нет ни при каком JOIN
    max_fanout: int  # наибольшее число строк ``to`` на одно значение ключа
    sums: tuple[SumChange, ...]

    @property
    def loses_rows(self) -> bool:
        return self.rows_inner < self.rows_after

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

    def to_contract(self) -> Relation:
        """Связь для ``DatasetCatalog.relations``; счётчики и проверка JOIN туда не входят."""
        return Relation(
            from_=ColumnRef(table=self.from_table, column=self.from_column),
            to=ColumnRef(table=self.to_table, column=self.to_column),
            overlap=self.overlap,
            jaccard=self.jaccard,
            cardinality=self.cardinality,
            confidence=self.confidence,
        )


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
    entity: str  # имя таблицы для сопоставления: из исходного файла, без суффиксов
    padded: bool  # есть значения с пробелами по краям
    trim: bool = False  # сравнение после trim — только для предупреждения о пробелах


def find_relations(connection: duckdb.DuckDBPyConnection) -> RelationReport:
    """Ищет связи по сохранённым профилям и записывает их; повторный вызов заменяет записи."""
    try:
        _ensure_table(connection)
        keys = _keys(connection, read_profiles(connection))
        relations, pair_warnings = [], []
        for a, b in combinations(keys, 2):
            if a.table.view_name == b.table.view_name:
                continue
            if _key_family(a) != _key_family(b):
                warning = _type_warning(connection, a, b)
                if warning:
                    pair_warnings.append(warning)
                continue
            relation = _relation(connection, a, b)
            if relation is not None:
                relations.append(relation)
            if a.padded or b.padded:
                warning = _padding_warning(connection, a, b, relation)
                if warning:
                    pair_warnings.append(warning)
        relations.sort(key=lambda r: (r.from_table, r.from_column, r.to_table, r.to_column))
        report = RelationReport(
            relations=tuple(relations),
            warnings=tuple(w for r in relations for w in relation_warnings(r))
            + tuple(pair_warnings),
        )
        connection.begin()
        try:
            _write_relations(connection, report)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except duckdb.Error as error:
        raise RelationError(f"не удалось посчитать ({error})") from error
    return report


def read_relations(connection: duckdb.DuckDBPyConnection) -> RelationReport:
    """Читает сохранённые связи и предупреждения последнего поиска."""
    _ensure_table(connection)
    rows = connection.execute(
        f"SELECT relation FROM {RELATIONS_TABLE} "
        "ORDER BY from_table, from_column, to_table, to_column"
    ).fetchall()
    warnings = connection.execute(
        f"SELECT warning FROM {WARNINGS_TABLE} ORDER BY position"
    ).fetchall()
    return RelationReport(
        relations=tuple(_relation_from_json(json.loads(row[0])) for row in rows),
        warnings=tuple(row[0] for row in warnings),
    )


def join_impact(
    connection: duckdb.DuckDBPyConnection,
    from_table: TableProfile,
    from_column: str,
    to_table: str,
    to_column: str,
) -> JoinImpact:
    """Число строк и суммы числовых колонок ``from`` до и после ``LEFT`` и ``INNER JOIN``.

    JOIN не материализуется: каждая строка ``from`` весит столько, сколько строк ``to``
    с её ключом (при LEFT JOIN строка без пары весит 1, при INNER — исключается). Суммы
    считаются в точном типе, как в профайлере; сумма без значений — ``None`` (SQL NULL).
    CTE выбирают только ключ и меры под служебными именами ``_djedai_*``, поэтому колонки
    таблиц (например, ``k`` или ``n``) с ними не пересекаются. Ключи должны быть одного
    семейства типов (``_key_family``): иначе строковое сравнение расходится с JOIN.
    """
    measures = [c for c in from_table.columns if c.semantic_type in NUMERIC]
    values = []
    for column in measures:
        wide = _wide_type(column.physical_type, column.semantic_type)
        values.append(f"CAST({_ident(column.name)} AS {wide})" if wide else _ident(column.name))
    selected = "".join(f", {v} AS _djedai_m{i}" for i, v in enumerate(values))
    sums = "".join(
        # INNER: у строки без пары _djedai_n IS NULL, произведение NULL и в сумму не входит —
        # если пар нет совсем, сумма NULL, как у настоящего JOIN, а не 0
        f", sum(f._djedai_m{i}), sum(f._djedai_m{i} * coalesce(m._djedai_n, 1)), "
        f"sum(f._djedai_m{i} * m._djedai_n)"
        for i in range(len(values))
    )
    row = connection.execute(
        f"""WITH m AS (
                SELECT {_key(to_column)} AS _djedai_k, count(*) AS _djedai_n
                FROM {_ident(to_table)} WHERE {_key(to_column)} IS NOT NULL GROUP BY 1),
            f AS (SELECT {_key(from_column)} AS _djedai_k{selected}
                FROM {_ident(from_table.view_name)})
            SELECT count(*), coalesce(sum(coalesce(m._djedai_n, 1)), 0),
                coalesce(sum(coalesce(m._djedai_n, 0)), 0),
                count(*) FILTER (WHERE f._djedai_k IS NOT NULL AND m._djedai_n IS NULL),
                count(*) FILTER (WHERE f._djedai_k IS NULL),
                coalesce(max(m._djedai_n), 0){sums}
            FROM f LEFT JOIN m ON f._djedai_k = m._djedai_k"""
    ).fetchone()
    rows_before, rows_after, rows_inner, unmatched, null_keys, fanout, *totals = row
    return JoinImpact(
        rows_before=rows_before,
        rows_after=int(rows_after),
        rows_inner=int(rows_inner),
        unmatched_rows=unmatched,
        null_key_rows=null_keys,
        max_fanout=fanout,
        sums=tuple(
            SumChange(c.name, *(_json_value(v) for v in totals[3 * i : 3 * i + 3]))
            for i, c in enumerate(measures)
        ),
    )


def relation_warnings(relation: TableRelation) -> tuple[str, ...]:
    """Предупреждения каталога: размножение строк, изменение сумм, потери при INNER JOIN."""
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
                f"{s.column} {_sum_text(s.before)} → {_sum_text(s.after)}"
                for s in impact.changed_sums
            )
        warnings.append(text)
    if impact.loses_rows:
        reasons = []
        if impact.unmatched_rows:
            reasons.append(f"без пары {impact.unmatched_rows}")
        if impact.null_key_rows:
            reasons.append(f"с пустым ключом {impact.null_key_rows}")
        lost = (impact.rows_after - impact.rows_inner) / impact.rows_after
        text = (
            f"INNER JOIN {link} теряет строки {relation.from_table} ({', '.join(reasons)}): "
            f"остаётся {impact.rows_inner} из {impact.rows_after} ({lost:.1%} потеряно)"
        )
        changed = [s for s in impact.sums if s.inner_after != s.after]
        if changed:
            text += "; суммы меняются: " + ", ".join(
                f"{s.column} {_sum_text(s.after)} → {_sum_text(s.inner_after)}" for s in changed
            )
        warnings.append(text)
    return tuple(warnings)


def _sum_text(value: object) -> str:
    return "нет значений" if value is None else str(value)


def _key(column: str, trim: bool = False) -> str:
    """Значение для сравнения — как в ``JOIN ON a.x = b.y``. Сравниваются только ключи
    одного семейства типов (``_key_family``), а внутри семейства строка значения однозначна,
    поэтому ``CAST`` в ``VARCHAR`` сравнения не меняет. ``trim`` — проверка совпадения без
    пробелов по краям, для предупреждения; сами связи по ней не строятся."""
    value = f"CAST({_ident(column)} AS VARCHAR)"
    return f"NULLIF(trim({value}), '')" if trim else value


def _key_family(key: _Key) -> str:
    """Семейство типа ключа: внутри семейства JOIN сравнивает значения так же, как строки.

    Целые разной ширины (``INTEGER`` и ``BIGINT``) — одно семейство. Текст и число — разные:
    ``JOIN ON число = текст`` приводит текст к числу, и ``'01'`` совпадает с ``1``, а на
    нечисловом значении JOIN падает. Строковое сравнение такие пары не описывает.
    """
    physical = key.column.physical_type
    return "integer" if physical in INTEGER_TYPES else physical


def _type_warning(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> str | None:
    """Предупреждение для пары ключей разных типов, похожей на связь по именам или значениям."""
    if not (_name_match(a, b) or _name_match(b, a) or _match(connection, a, b)):
        return None
    left, right = sorted((a, b), key=lambda k: (k.table.view_name, k.column.name))
    return (
        f"{left.table.view_name}.{left.column.name} ({left.column.physical_type}) и "
        f"{right.table.view_name}.{right.column.name} ({right.column.physical_type}) похожи "
        "на связь, но типы ключей разные: JOIN приводит текст к числу ('01' совпадает с 1, "
        "нечисловое значение даёт ошибку), поэтому связь не проверялась и не предлагается. "
        "Приведите ключи к одному типу явно"
    )


def _is_candidate(table: TableProfile, column: ColumnProfile) -> bool:
    if column.semantic_type == "id":
        return True
    return column.semantic_type in KEY_TYPES and (
        bool(_ID_NAME.search(column.name)) or column.name in table.primary_key_candidates
    )


def _entities(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """``raw_table → имя сущности`` из исходного имени файла или таблицы подключённой БД.

    Имя представления может получить технический суффикс (``clients_typed`` при занятом
    ``clients``, ``raw_clients_2`` при повторе файла) — он не должен менять распознавание.
    """
    entities = {
        source.raw_table: slugify(Path(source.original_name).stem)
        for source in read_sources(connection)
    }
    entities |= {
        source.raw_table: slugify(source.relation.rpartition(".")[2])
        for source in read_db_sources(connection)
    }
    return entities


def _keys(connection: duckdb.DuckDBPyConnection, profiles: list[TableProfile]) -> list[_Key]:
    entities = _entities(connection)
    keys = []
    for table in profiles:
        entity = entities.get(table.raw_table, table.view_name)
        for column in table.columns:
            if not _is_candidate(table, column):
                continue
            present, distinct, padded = connection.execute(
                f"SELECT count({_key(column.name)}), count(DISTINCT {_key(column.name)}), "
                f"count(*) FILTER (WHERE {_key(column.name)} <> trim({_key(column.name)})) "
                f"FROM {_ident(table.view_name)}"
            ).fetchone()
            if distinct >= MIN_DISTINCT:
                keys.append(_Key(table, column, distinct, present == distinct, entity, padded > 0))
    return keys


def _trimmed(connection: duckdb.DuckDBPyConnection, key: _Key) -> _Key:
    """Тот же кандидат при сравнении без пробелов по краям."""
    if not key.padded:
        return replace(key, trim=True)
    value = _key(key.column.name, trim=True)
    present, distinct = connection.execute(
        f"SELECT count({value}), count(DISTINCT {value}) FROM {_ident(key.table.view_name)}"
    ).fetchone()
    return replace(key, distinct=distinct, unique=present == distinct, trim=True)


def _padding_warning(
    connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key, exact: TableRelation | None
) -> str | None:
    """Предупреждение, если без пробелов по краям связь была бы сильнее, чем по точным
    значениям: настоящий JOIN без ``trim`` такие строки не соединит."""
    trimmed = _match(connection, _trimmed(connection, a), _trimmed(connection, b))
    if trimmed is None:
        return None
    source, target, _, _, overlap, _, _ = trimmed
    if exact is not None and exact.overlap >= round(overlap, 4):
        return None
    padded = [f"{k.table.view_name}.{k.column.name}" for k in (source, target) if k.padded]
    found = f"overlap {exact.overlap}" if exact is not None else "связь не найдена"
    return (
        f"{source.table.view_name}.{source.column.name} и "
        f"{target.table.view_name}.{target.column.name} совпадают только без пробелов "
        f"по краям значений (в {', '.join(padded)}): по точным значениям {found}, "
        f"без пробелов overlap {round(overlap, 4)}. JOIN по этим колонкам без trim "
        "теряет строки"
    )


def _intersection(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> int:
    def values(key: _Key) -> str:
        value = _key(key.column.name, key.trim)
        return f"SELECT DISTINCT {value} AS k FROM {_ident(key.table.view_name)}"

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
        return left in _table_stems(target.entity)
    if not left and right:
        return right in _table_stems(source.entity)
    return False


def _match(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> tuple | None:
    """Меры пересечения принятой пары: ``(from, to, кратность, ∩, overlap, jaccard, имена)``."""
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
    return source, target, cardinality, common, overlap, jaccard, named


def _relation(connection: duckdb.DuckDBPyConnection, a: _Key, b: _Key) -> TableRelation | None:
    found = _match(connection, a, b)
    if found is None:
        return None
    source, target, cardinality, common, overlap, jaccard, named = found
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
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {WARNINGS_TABLE} (position INTEGER PRIMARY KEY, "
        "warning VARCHAR)"
    )


def _write_relations(connection: duckdb.DuckDBPyConnection, report: RelationReport) -> None:
    for table in (RELATIONS_TABLE, WARNINGS_TABLE):
        connection.execute(f"DELETE FROM {table}")
    for position, warning in enumerate(report.warnings, start=1):
        connection.execute(f"INSERT INTO {WARNINGS_TABLE} VALUES (?, ?)", [position, warning])
    for relation in report.relations:
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
