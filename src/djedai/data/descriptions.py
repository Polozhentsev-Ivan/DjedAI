"""LLM-описания каталога (B1-6): колонки, таблицы и краткое описание датасета.

Модель получает компактный JSON по каждой таблице — имена, оригинальные заголовки, тип,
роль, примеры и статистику (примеры и частые значения — только при ``ALLOW_DATA_SHARING``) —
и одним вызовом описывает таблицу и её колонки (широкие
таблицы делятся на части по ``MAX_COLUMNS_PER_CALL``). Затем одним вызовом пишется
``summary`` датасета по описаниям таблиц, периодам и связям. Заполняются только пустые
поля: описания, которые уже есть в каталоге, не перезаписываются.

Принятые ответы кэшируются в ``_djedai_descriptions``; отвергнутые и неполные — нет,
чтобы пустые поля можно было получить при следующем вызове. Ключ — SHA-256 от имени
модели, текста промпта, схемы ответа и входного JSON, поэтому повторное чтение не
обращается к модели, а при изменении данных, промпта, схемы или модели описание
пересчитывается.

Ошибка модели не ломает каталог: таблицы и статистика остаются как были, в ``warnings``
добавляется причина. Если модель недоступна (таймаут, ошибка сервера, исключение
клиента), остальные вызовы не делаются.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import duckdb
from pydantic import BaseModel, ConfigDict

from djedai.agent.client import MODEL_UNAVAILABLE
from djedai.agent.config import data_sharing_allowed
from djedai.agent.prompts import load_prompt
from djedai.contracts.catalog import Column, DatasetCatalog, Table

if TYPE_CHECKING:
    from djedai.agent.client import ModelResponse

DESCRIPTIONS_TABLE = "_djedai_descriptions"
TABLE_PROMPT = "describe_table"
DATASET_PROMPT = "describe_dataset"
MAX_COLUMNS_PER_CALL = 20  # ~1000 токенов ответа: укладывается в таймаут локальной модели
MAX_DESCRIPTION_CHARS = 200
MAX_SUMMARY_CHARS = 400
TOP_VALUES = 5
UNAVAILABLE = (*MODEL_UNAVAILABLE, "CLIENT_ERROR")  # дальше вызывать модель незачем
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class ModelClient(Protocol):  # BIModelClient из djedai.agent.client
    model: str

    def generate_typed_response[T: BaseModel](
        self, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> ModelResponse[T]: ...


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnDescription(_Answer):
    name: str
    description: str


class TableDescriptions(_Answer):
    table: str
    columns: list[ColumnDescription]


class DatasetSummary(_Answer):
    summary: str


@dataclass(frozen=True)
class DescriptionReport:
    catalog: DatasetCatalog
    warnings: tuple[str, ...]
    model_calls: int  # 0 — всё взято из кэша или описывать было нечего


class _ModelUnavailable(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class _Describer:
    def __init__(
        self, connection: duckdb.DuckDBPyConnection, client: ModelClient, share_values: bool
    ) -> None:
        self.connection = connection
        self.client = client
        self.share_values = share_values
        self.calls = 0

    def ask[T: _Answer](
        self, prompt: str, payload: dict, answer: type[T], complete: Callable[[T], bool]
    ) -> T | str:
        """Ответ из кэша или модели; при ошибке — её тип (``INVALID_JSON``, ...).

        В кэш попадает только ответ, для которого ``complete`` истинно: все нужные описания
        прошли проверку. Отвергнутый или неполный ответ не кэшируется, поэтому при
        следующем вызове пустые поля можно получить заново.
        """
        user = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        system = load_prompt(prompt)
        schema = json.dumps(answer.model_json_schema(), sort_keys=True)
        key = hashlib.sha256(
            "\n".join((self.client.model, prompt, system, schema, user)).encode()
        ).hexdigest()
        cached = self.connection.execute(
            f"SELECT payload FROM {DESCRIPTIONS_TABLE} WHERE key = ?", [key]
        ).fetchone()
        if cached:
            return answer.model_validate_json(cached[0])
        self.calls += 1
        try:
            response = self.client.generate_typed_response(system, user, answer)
        except Exception as error:  # клиент не должен ронять загрузку данных
            raise _ModelUnavailable("CLIENT_ERROR") from error
        if response.status != "success" or response.data is None:
            error_type = response.error_type or "UNKNOWN_ERROR"
            if error_type in UNAVAILABLE:
                raise _ModelUnavailable(error_type)
            return error_type
        if complete(response.data):
            self.connection.execute(
                f"INSERT OR REPLACE INTO {DESCRIPTIONS_TABLE} (key, kind, payload) "
                "VALUES (?, ?, ?)",
                [key, prompt, response.data.model_dump_json()],
            )
        return response.data


def describe_catalog(
    connection: duckdb.DuckDBPyConnection,
    catalog: DatasetCatalog,
    client: ModelClient,
    *,
    share_values: bool = True,
) -> DescriptionReport:
    """Копия каталога с описаниями колонок, таблиц и ``summary``; сбой модели — в warnings.

    Значения ячеек (примеры и частые значения) уходят модели, только если администратор
    разрешил это явно (``ALLOW_DATA_SHARING=true``, см. ``data_sharing_allowed``): по
    умолчанию ``MODEL_BASE_URL`` считается внешним API. ``share_values=False`` запрещает
    передачу и при разрешении, но разрешить её в обход настройки нельзя. Без значений
    остаются имена, типы и агрегаты; описания при этом обычно менее точны.
    """
    _ensure_table(connection)
    describer = _Describer(connection, client, share_values and data_sharing_allowed())
    warnings: list[str] = []
    tables = list(catalog.tables)
    summary = catalog.summary
    try:
        for index, table in enumerate(tables):
            tables[index] = _describe_table(describer, table, warnings)
        if summary is None and tables:
            summary = _describe_dataset(describer, catalog, tables, warnings)
    except _ModelUnavailable as error:
        warnings.append(
            f"Описания данных не сгенерированы: модель недоступна ({error.error_type}); "
            "структура и статистика каталога доступны"
        )
    new_warnings = [w for w in warnings if w not in catalog.warnings]
    described = catalog.model_copy(
        update={
            "tables": tables,
            "summary": summary,
            "warnings": [*catalog.warnings, *new_warnings],
        }
    )
    return DescriptionReport(described, tuple(warnings), describer.calls)


def _describe_table(describer: _Describer, table: Table, warnings: list[str]) -> Table:
    if table.description is not None and all(c.description is not None for c in table.columns):
        return table
    texts: dict[str, str] = {}
    table_text: str | None = None
    rejected: list[str] = []
    for start in range(0, len(table.columns), MAX_COLUMNS_PER_CALL):
        part = table.columns[start : start + MAX_COLUMNS_PER_CALL]
        needs_table = start == 0 and table.description is None
        answer = describer.ask(
            TABLE_PROMPT,
            _table_payload(table, part, describer.share_values),
            TableDescriptions,
            lambda found, part=part, needs_table=needs_table: _complete_table(
                found, part, needs_table
            ),
        )
        if isinstance(answer, str):
            warnings.append(
                f"Описания таблицы {table.table_id} не сгенерированы: ответ модели "
                f"не принят ({answer})"
            )
            return table
        if table_text is None:
            table_text = _clean(answer.table, MAX_DESCRIPTION_CHARS)
        for name, text in _accepted_columns(answer, part).items():
            texts.setdefault(name, text)
    columns = []
    for column in table.columns:
        if column.description is None and column.name not in texts:
            rejected.append(column.name)
        columns.append(_with_description(column, texts.get(column.name)))
    if table.description is None and table_text is None:
        rejected.insert(0, "описание таблицы")
    if rejected:
        warnings.append(f"Модель не описала в {table.table_id}: {', '.join(rejected)}")
    return table.model_copy(
        update={"description": table.description or table_text, "columns": columns}
    )


def _accepted_columns(answer: TableDescriptions, part: Sequence[Column]) -> dict[str, str]:
    """Принятые описания колонок части: известное имя и текст на русском."""
    names = {c.name for c in part}
    accepted: dict[str, str] = {}
    for item in answer.columns:
        text = _clean(item.description, MAX_DESCRIPTION_CHARS)
        if item.name in names and text is not None:
            accepted.setdefault(item.name, text)
    return accepted


def _complete_table(answer: TableDescriptions, part: Sequence[Column], needs_table: bool) -> bool:
    """Ответ можно кэшировать: описаны таблица (если нужна) и все пустые колонки части."""
    if needs_table and _clean(answer.table, MAX_DESCRIPTION_CHARS) is None:
        return False
    accepted = _accepted_columns(answer, part)
    return all(c.name in accepted for c in part if c.description is None)


def _with_description(column: Column, text: str | None) -> Column:
    if column.description is not None or text is None:
        return column
    return column.model_copy(update={"description": text})


def _describe_dataset(
    describer: _Describer, catalog: DatasetCatalog, tables: list[Table], warnings: list[str]
) -> str | None:
    answer = describer.ask(
        DATASET_PROMPT,
        _dataset_payload(catalog, tables),
        DatasetSummary,
        lambda found: _clean(found.summary, MAX_SUMMARY_CHARS) is not None,
    )
    if isinstance(answer, str):
        warnings.append(f"Краткое описание датасета не сгенерировано: ответ не принят ({answer})")
        return None
    summary = _clean(answer.summary, MAX_SUMMARY_CHARS)
    if summary is None:
        warnings.append("Краткое описание датасета не сгенерировано: ответ не на русском")
    return summary


def _clean(text: str, limit: int) -> str | None:
    """Описание на русском без лишних пробелов; длинное — обрезается по слову."""
    text = " ".join(text.split())
    if not text or not _CYRILLIC.search(text):
        return None
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    return text


def _table_payload(
    table: Table, columns: tuple[Column, ...] | list[Column], share_values: bool = True
) -> dict:
    """Вход модели: структура и статистика без описаний, журнала и сведений о файле."""
    head = {
        "table": table.table_id,
        "original_name": table.original_name,
        "row_count": table.row_count,
        "primary_key_candidates": table.primary_key_candidates,
        "time_columns": table.time_columns,
    }
    return head | {"columns": [_column_payload(c, share_values) for c in columns]}


def _column_payload(column: Column, share_values: bool = True) -> dict:
    stats = {
        k: v
        for k, v in column.stats.model_dump(include={"min", "max", "granularity"}).items()
        if v is not None
    }
    payload = {
        "name": column.name,
        "original_name": column.original_name,
        "semantic_type": column.semantic_type,
        "role": column.role,
        "currency": column.currency,
        "null_ratio": round(column.null_ratio, 4),
        "distinct_count": column.distinct_count,
        "is_unique": column.is_unique,
        "sample_values": column.sample_values if share_values else None,
        "top_values": (
            [t.model_dump() for t in column.top_values[:TOP_VALUES]] if share_values else None
        ),
        "stats": stats,
    }
    return {k: v for k, v in payload.items() if v not in (None, [], {})}


def _dataset_payload(catalog: DatasetCatalog, tables: list[Table]) -> dict:
    def periods(table: Table) -> list[dict]:
        result = []
        for name in table.time_columns:
            column = table.column(name)
            if column is not None and column.stats.min is not None:
                result.append({"column": name, "min": column.stats.min, "max": column.stats.max})
        return result

    return {
        "dataset": catalog.name,
        "source": catalog.source.kind,
        "tables": [
            {
                "table": t.table_id,
                "original_name": t.original_name,
                "description": t.description,
                "row_count": t.row_count,
                "periods": periods(t),
            }
            for t in tables
        ],
        "relations": [
            f"{r.from_.table}.{r.from_.column} → {r.to.table}.{r.to.column} ({r.cardinality})"
            for r in catalog.relations
        ],
    }


def _ensure_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {DESCRIPTIONS_TABLE} (
            key VARCHAR PRIMARY KEY, kind VARCHAR, payload VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp)"""
    )
