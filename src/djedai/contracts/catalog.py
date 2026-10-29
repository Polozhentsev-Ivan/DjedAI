"""Контракт ``DatasetCatalog`` (B1-7): структура загруженных данных для всех компонентов.

Полный каталог (``model_dump(mode="json")``) нужен интерфейсу и инструменту
``describe_column``; компактный ``to_prompt()`` передаётся агенту: таблицы, колонки
с типом, ролью и описанием, связи — без top-values и примеров значений.
Формат описан в спецификации (§3.4); примеры лежат в ``contracts/examples/``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1
MAX_TOP_VALUES = 10
MAX_SAMPLE_VALUES = 5

SemanticType = Literal[
    "id", "integer", "float", "money", "percent", "datetime", "date", "category", "text", "bool"
]
Role = Literal["id", "measure", "dimension", "time", "text"]
SourceKind = Literal["csv_upload", "postgres", "clickhouse"]
Granularity = Literal["second", "minute", "hour", "day", "week", "month", "quarter", "year"]
Cardinality = Literal["1:1", "1:N", "N:1", "N:M"]
Confidence = Literal["high", "medium", "low"]
Value = bool | int | float | str  # значение ячейки в JSON; даты — ISO-строки

NUMERIC: tuple[SemanticType, ...] = ("integer", "float", "money", "percent")
TEMPORAL: tuple[SemanticType, ...] = ("date", "datetime")

Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]  # ISO 4217, например RUB
Count = Annotated[int, Field(ge=0)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class DatasetSource(_Model):
    kind: SourceKind


class TableSource(_Model):  # для CSV — файл и диалект, для БД — исходная таблица
    file: str | None = None
    encoding: str | None = None
    delimiter: str | None = None
    header: bool | None = None
    relation: str | None = None  # schema.table в исходной БД


class TopValue(_Model):
    value: Value
    count: int = Field(ge=1)


class ColumnStats(_Model):  # числа: min/max/mean/median/std; даты: min/max/granularity
    min: Value | None = None
    max: Value | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    granularity: Granularity | None = None


class ParseInfo(_Model):  # как значения приведены к типу (журнал B1-2)
    format: str | None = None
    success_ratio: Ratio  # доля распознанных среди непустых значений
    failed_examples: list[str] = Field(default_factory=list, max_length=MAX_SAMPLE_VALUES)


class Column(_Model):
    name: str = Field(min_length=1)
    original_name: str
    physical_type: str
    semantic_type: SemanticType
    role: Role
    null_ratio: Ratio  # доля пустых значений в исходных данных, без нераспознанных
    distinct_count: Count
    is_unique: bool
    sample_values: list[Value] = Field(default_factory=list, max_length=MAX_SAMPLE_VALUES)
    top_values: list[TopValue] = Field(default_factory=list, max_length=MAX_TOP_VALUES)
    stats: ColumnStats = Field(default_factory=ColumnStats)
    parse: ParseInfo | None = None
    currency: CurrencyCode | None = None  # только у money; без него валюта неизвестна
    description: str | None = None  # LLM (B1-6)

    @model_validator(mode="after")
    def _stats_match_type(self) -> Column:
        if self.currency is not None and self.semantic_type != "money":
            raise ValueError(f"{self.name}: currency есть только у денежных колонок")
        numeric = (self.stats.mean, self.stats.median, self.stats.std)
        if self.semantic_type not in NUMERIC and any(v is not None for v in numeric):
            raise ValueError(f"{self.name}: mean/median/std есть только у числовых колонок")
        if self.semantic_type not in TEMPORAL and self.stats.granularity is not None:
            raise ValueError(f"{self.name}: granularity есть только у дат")
        if self.semantic_type not in NUMERIC + TEMPORAL and (
            self.stats.min is not None or self.stats.max is not None
        ):
            raise ValueError(f"{self.name}: min/max есть только у чисел и дат")
        return self


class Table(_Model):
    table_id: str = Field(min_length=1)  # имя представления в DuckDB
    raw_table: str | None = None  # для подключённых БД сырой таблицы нет
    original_name: str
    description: str | None = None  # LLM (B1-6)
    row_count: Count
    source: TableSource = Field(default_factory=TableSource)
    primary_key_candidates: list[str] = Field(default_factory=list)
    time_columns: list[str] = Field(default_factory=list)
    duplicate_rows: Count
    columns: list[Column] = Field(min_length=1)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @model_validator(mode="after")
    def _columns_are_consistent(self) -> Table:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.table_id}: имена колонок повторяются")
        for field in ("primary_key_candidates", "time_columns"):
            unknown = [n for n in getattr(self, field) if n not in names]
            if unknown:
                raise ValueError(f"{self.table_id}.{field}: нет колонок {unknown}")
        if self.duplicate_rows > self.row_count:
            raise ValueError(f"{self.table_id}: дублей больше, чем строк")
        return self


class ColumnRef(_Model):
    table: str
    column: str


class Relation(_Model):  # связь по ID (B1-5)
    from_: ColumnRef = Field(alias="from")
    to: ColumnRef
    overlap: Ratio
    jaccard: Ratio
    cardinality: Cardinality
    confidence: Confidence


class Transformation(_Model):  # запись журнала преобразований (B1-2)
    table: str
    column: str | None = None
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)


class CatalogRevision(_Model):  # версия профиля и схемы источника (B1-11, #169)
    number: int = Field(ge=1)  # растёт при каждой перепрофилировке
    profiled_at: AwareDatetime
    schema_fingerprint: Sha256  # таблица источника, колонка и тип в порядке колонок
    source_fingerprint: Sha256  # источник без пользователя, пароля и адреса в открытом виде
    transform_version: str = Field(min_length=1)  # версия правил типизации


class DatasetCatalog(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    dataset_id: str = Field(min_length=1)
    name: str
    created_at: AwareDatetime
    source: DatasetSource
    revision: CatalogRevision | None = None  # None — каталог сохранён до #169
    summary: str | None = None  # LLM (B1-6)
    tables: list[Table] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    transformations: list[Transformation] = Field(default_factory=list)

    def table(self, table_id: str) -> Table | None:
        return next((t for t in self.tables if t.table_id == table_id), None)

    @model_validator(mode="after")
    def _references_exist(self) -> DatasetCatalog:
        ids = [t.table_id for t in self.tables]
        if len(set(ids)) != len(ids):
            raise ValueError("table_id повторяются")
        for relation in self.relations:
            for ref in (relation.from_, relation.to):
                table = self.table(ref.table)
                if table is None or table.column(ref.column) is None:
                    raise ValueError(
                        f"связь ссылается на несуществующую колонку {ref.table}.{ref.column}"
                    )
        for entry in self.transformations:
            table = self.table(entry.table)
            if table is None or (entry.column is not None and table.column(entry.column) is None):
                raise ValueError(
                    f"преобразование ссылается на несуществующую колонку "
                    f"{entry.table}.{entry.column}"
                )
        return self

    def to_prompt(self) -> str:
        """Компактное описание для агента: без top-values, примеров и журнала."""
        lines = [f"Датасет «{self.name}» (id {self.dataset_id})."]
        if self.summary:
            lines.append(self.summary)
        lines.append(f"Колонки: {_COLUMN_FORMAT}.")
        foreign = {(r.from_.table, r.from_.column): r.to for r in self.relations}
        for table in self.tables:
            lines += ["", _table_line(table)]
            lines += [
                _column_line(column, _key_marks(table, column, foreign)) for column in table.columns
            ]
        if self.relations:
            lines += ["", "Связи:"]
            lines += [
                f"- {r.from_.table}.{r.from_.column} → {r.to.table}.{r.to.column} "
                f"({r.cardinality}, уверенность {r.confidence})"
                for r in self.relations
            ]
        if self.warnings:
            lines += ["", "Предупреждения:", *(f"- {w}" for w in self.warnings)]
        return "\n".join(lines)


_COLUMN_FORMAT = (
    "имя: тип, роль, ключ (PK, FK → таблица.колонка), валюта, «исходное имя», "
    "доля пропусков, доля нераспознанных, число уникальных, "
    "диапазон в данных — описание"
)
_DISTINCT_TYPES: tuple[SemanticType, ...] = ("category", "bool", "text")


def _table_line(table: Table) -> str:
    parts = [f"строк {table.row_count}"]
    if table.primary_key_candidates:
        parts.append(
            "ключ " + ", ".join(_key_name(table, key) for key in table.primary_key_candidates)
        )
    if table.time_columns:
        parts.append("время " + ", ".join(table.time_columns))
    if table.duplicate_rows:
        parts.append(f"полных дублей {table.duplicate_rows}")
    head = f"Таблица {table.table_id}"
    if table.original_name != table.table_id:
        head += f" («{table.original_name}»)"
    if table.description:
        head += f" — {table.description}"
    return f"{head}; {'; '.join(parts)}:"


def _key_name(table: Table, key: str) -> str:
    column = table.column(key)
    return key if column is None or column.is_unique else f"{key} (не уникален)"


def _key_marks(table: Table, column: Column, foreign: dict[tuple[str, str], ColumnRef]) -> str:
    """``PK`` для ключа таблицы и ``FK → таблица.колонка`` для ссылки на ключ (и своей таблицы)."""
    marks = []
    if column.name in table.primary_key_candidates:
        marks.append("PK")
    target = foreign.get((table.table_id, column.name))
    if target is not None:
        marks.append(f"FK → {target.table}.{target.column}")
    return ", ".join(marks)


def _column_line(column: Column, key_marks: str = "") -> str:
    line = f"- {column.name}: {column.semantic_type}, {column.role}"
    if key_marks:
        line += f", {key_marks}"
    if column.currency:
        line += f", валюта {column.currency}"
    if _shows_original_name(column):
        line += f", «{column.original_name}»"
    if column.null_ratio:
        line += f", пропусков {column.null_ratio:.1%}"
    if (failed := _parse_failed_ratio(column)) and f"{failed:.1%}" != "0.0%":
        line += f", не распознано {failed:.1%}"
    if column.semantic_type in _DISTINCT_TYPES:
        line += f", уникальных {column.distinct_count}"
    if column.semantic_type in NUMERIC + TEMPORAL and column.stats.min is not None:
        line += f", {_value(column.stats.min)} … {_value(column.stats.max)}"
        if column.stats.granularity:
            line += f" ({column.stats.granularity})"
    if column.description:
        line += f" — {column.description}"
    return line


def _parse_failed_ratio(column: Column) -> float:
    """Доля нераспознанных значений от всех строк; ``success_ratio`` считается по непустым."""
    if column.parse is None:
        return 0.0
    return (1 - column.parse.success_ratio) * (1 - column.null_ratio)


def _shows_original_name(column: Column) -> bool:
    """Исходный заголовок нужен агенту, если описание его не передаёт."""
    if column.original_name == column.name:
        return False
    if not column.description:
        return True
    return column.original_name.casefold() not in column.description.casefold()


def _value(value: Value | None) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
