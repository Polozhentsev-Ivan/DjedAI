"""Репозитории и сервисы хранения данных приложения (§7.1, B5-2).

Обеспечивают:
- Сохранение и чтение вызовов инструментов (ToolResult со статусами ok, error, empty)
- Хранение больших данных по data_ref в формате Parquet через DuckDB
- Сохранение и чтение пользователей, датасетов, чатов, сообщений и дашбордов
- Поддержание связей владения (owner_id) между сущностями
"""

from __future__ import annotations

import copy
import logging
import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from djedai.backend.chat_turns import apply_turn
from djedai.backend.config import get_settings
from djedai.backend.db.models import (
    ChatModel,
    DashboardModel,
    DatasetModel,
    MessageModel,
    ToolResultModel,
    UserModel,
)
from djedai.backend.db.session import budgeted_write
from djedai.contracts.chat import ChatTurnState
from djedai.contracts.freshness import ReadProvenance
from djedai.contracts.message import AssistantMessage
from djedai.contracts.metrics import MetricProvenance
from djedai.contracts.quality import QualityReport
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultColumn,
    ToolResultData,
)
from djedai.execution import ExecutionBudget, TurnCancelled

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _escape_sql_ident(ident: str) -> str:
    """Экранирует SQL-идентификатор для DuckDB, удваивая двойные кавычки."""
    return '"' + str(ident).replace('"', '""') + '"'


def _escape_sql_literal(literal: str) -> str:
    """Экранирует строковый литерал (путь к файлу) для DuckDB, удваивая апострофы."""
    return "'" + str(literal).replace("'", "''") + "'"


def _sanitize_for_json(obj: Any) -> Any:
    """Рекурсивно приводит объекты (Decimal, date, datetime и др.) к типам JSON."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, (Path, uuid.UUID)):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _sanitize_for_json(obj.model_dump())
    if hasattr(obj, "value"):
        return _sanitize_for_json(obj.value)
    return str(obj)


def _normalize_columns(raw_columns: list[Any]) -> list[str]:
    """Приводит список колонок к строковым именам."""
    names: list[str] = []
    for c in raw_columns:
        if isinstance(c, str):
            names.append(c)
        elif hasattr(c, "name"):
            names.append(str(c.name))
        elif isinstance(c, dict) and "name" in c:
            names.append(str(c["name"]))
        else:
            names.append(str(c))
    return names


def _infer_duckdb_type(col_meta: Any, values: list[Any]) -> str:
    """Определяет точный DuckDB-тип для всей колонки без потери точности."""
    non_nulls = [v for v in values if v is not None]

    if isinstance(col_meta, ToolResultColumn) or (
        isinstance(col_meta, dict) and "type" in col_meta
    ):
        meta_type = (
            col_meta.type if isinstance(col_meta, ToolResultColumn) else col_meta.get("type")
        )
        if meta_type:
            m = str(meta_type).lower()
            dec_match = re.match(r"^(?:decimal|numeric)\((\d+),\s*(\d+)\)$", m)
            if dec_match:
                return f"DECIMAL({dec_match.group(1)},{dec_match.group(2)})"
            if m in ("decimal", "numeric", "money"):
                scales = [
                    -v.as_tuple().exponent
                    for v in non_nulls
                    if isinstance(v, Decimal)
                    and isinstance(v.as_tuple().exponent, int)
                    and v.as_tuple().exponent < 0
                ]
                scale = max(scales) if scales else (2 if m == "money" else 2)
                return f"DECIMAL(38,{scale})"
            if m in ("number", "float", "double", "real"):
                decimals = [v for v in non_nulls if isinstance(v, Decimal)]
                if decimals:
                    scales = [
                        -v.as_tuple().exponent
                        for v in decimals
                        if isinstance(v.as_tuple().exponent, int) and v.as_tuple().exponent < 0
                    ]
                    scale = max(scales) if scales else 2
                    return f"DECIMAL(38,{scale})"
                return "DOUBLE"
            if m in ("int", "integer", "bigint", "smallint"):
                return "BIGINT"
            if m in ("date",):
                return "DATE"
            if m in ("datetime", "timestamp"):
                return "TIMESTAMP"
            if m in ("bool", "boolean"):
                return "BOOLEAN"

    if not non_nulls:
        return "VARCHAR"

    has_str = False
    has_float = False
    has_decimal = False
    has_int = False
    has_bool = False
    has_datetime = False
    has_date = False

    for v in non_nulls:
        if isinstance(v, bool):
            has_bool = True
        elif isinstance(v, int):
            has_int = True
        elif isinstance(v, Decimal):
            has_decimal = True
        elif isinstance(v, float):
            has_float = True
        elif isinstance(v, datetime):
            has_datetime = True
        elif isinstance(v, date):
            has_date = True
        else:
            has_str = True

    if has_str:
        return "VARCHAR"
    if has_datetime:
        return "TIMESTAMP"
    if has_date and not (has_int or has_float or has_decimal or has_bool):
        return "DATE"
    if has_decimal and not (has_str or has_datetime or has_date or has_bool or has_float):
        scales = [
            -v.as_tuple().exponent
            for v in non_nulls
            if isinstance(v, Decimal)
            and isinstance(v.as_tuple().exponent, int)
            and v.as_tuple().exponent < 0
        ]
        scale = max(scales) if scales else 0
        return f"DECIMAL(38,{scale})"
    if has_float:
        return "DOUBLE"
    if has_int and not has_bool:
        return "BIGINT"
    if has_bool and not has_int:
        return "BOOLEAN"
    if has_int and has_bool:
        return "BIGINT"

    return "VARCHAR"


def write_parquet(
    file_path: Path | str,
    columns: list[str],
    rows: list[list[Any]],
    columns_meta: list[Any] | None = None,
) -> Path:
    """Записывает колонки и строки в Parquet-файл через встроенный движок DuckDB.

    Использует атомарную запись через временный файл (.tmp), предотвращая
    появление поврежденных файлов при сбоях.
    """
    path = Path(file_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp_{uuid.uuid4().hex}")
    duck_path = tmp_path.as_posix()

    con = duckdb.connect()
    try:
        escaped_path = _escape_sql_literal(duck_path)
        meta_list = columns_meta or [None] * len(columns)

        if not columns:
            raise ValueError(
                "Невозможно записать Parquet-файл без колонок: требуется минимум одна колонка"
            )

        if not rows:
            col_defs = ", ".join(f"{_escape_sql_ident(col)} VARCHAR" for col in columns)
            con.execute(f"CREATE TEMPORARY TABLE _res_empty ({col_defs})")
            con.execute(f"COPY _res_empty TO {escaped_path} (FORMAT PARQUET)")
            tmp_path.replace(path)
            return path

        col_types = []
        for i, _col in enumerate(columns):
            col_values = [r[i] for r in rows if i < len(r)]
            t = _infer_duckdb_type(meta_list[i] if i < len(meta_list) else None, col_values)
            col_types.append(t)

        col_defs = ", ".join(
            f"{_escape_sql_ident(col)} {t}" for col, t in zip(columns, col_types, strict=False)
        )
        placeholders = ", ".join(["?"] * len(columns))
        con.execute(f"CREATE TEMPORARY TABLE _res_data ({col_defs})")

        formatted_rows = []
        for r in rows:
            row_data = []
            for i in range(len(columns)):
                v = r[i] if i < len(r) else None
                t = col_types[i]
                if v is None:
                    row_data.append(None)
                elif t.startswith("DECIMAL"):
                    row_data.append(Decimal(str(v)) if not isinstance(v, Decimal) else v)
                elif t == "DOUBLE":
                    row_data.append(float(v))
                elif t == "BIGINT":
                    row_data.append(int(v))
                elif t == "BOOLEAN":
                    row_data.append(bool(v))
                elif t in ("DATE", "TIMESTAMP"):
                    row_data.append(v.isoformat() if hasattr(v, "isoformat") else str(v))
                elif isinstance(v, (dict, list)):
                    row_data.append(str(v))
                else:
                    row_data.append(v)
            formatted_rows.append(row_data)

        con.executemany(f"INSERT INTO _res_data VALUES ({placeholders})", formatted_rows)
        con.execute(f"COPY _res_data TO {escaped_path} (FORMAT PARQUET)")
        tmp_path.replace(path)
        return path
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        con.close()


def read_parquet(file_path: Path | str) -> tuple[list[str], list[list[Any]]]:
    """Читает колонки и строки из Parquet-файла через DuckDB."""
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Parquet-файл не найден: {path}")

    duck_path = path.as_posix()
    escaped_path = _escape_sql_literal(duck_path)
    con = duckdb.connect()
    try:
        res = con.execute(f"SELECT * FROM {escaped_path}").fetchall()
        cols = [desc[0] for desc in con.description]
        rows = [list(r) for r in res]
        return cols, rows
    finally:
        con.close()


SAFE_DATA_REF_RE = re.compile(r"^[a-zA-Z0-9_\-]+\.parquet$")


def _resolve_data_ref_path(
    data_ref: str,
    base_dir: Path | str | None = None,
    expected_result_id: str | None = None,
) -> Path:
    """Разрешает безопасный путь к файлу данных из data_ref ссылки внутри хранилища."""
    if not data_ref.startswith("parquet://"):
        raise ValueError(
            f"Security error: unsupported data_ref scheme in '{data_ref}', expected 'parquet://'"
        )

    ref = data_ref.removeprefix("parquet://")

    # Строгая защита от Path Traversal: запрет разделителей каталогов и '..'
    if ".." in ref or "/" in ref or "\\" in ref:
        raise ValueError(
            "Security error: directory traversal or path separators not allowed in data_ref: "
            f"'{data_ref}'"
        )

    # Имя файла должно соответствовать строгому шаблону (полное совпадение)
    if not SAFE_DATA_REF_RE.fullmatch(ref):
        raise ValueError(
            f"Security error: invalid data_ref filename '{ref}', expected [a-zA-Z0-9_-]+.parquet"
        )

    # Защита от подмены файла: имя обязано совпадать с ожидаемым result_id
    if expected_result_id is not None:
        expected_name = f"{expected_result_id}.parquet"
        if ref != expected_name:
            raise ValueError(
                f"Security error: data_ref '{ref}' does not match "
                f"expected result_id '{expected_result_id}'"
            )

    base = Path(base_dir or get_settings().results_data_dir).resolve()
    original = base / ref
    if original.is_symlink():
        raise ValueError("Security error: symbolic data_ref links are not allowed")
    candidate = original.resolve()

    if not candidate.is_relative_to(base):
        raise ValueError(
            f"Security error: data_ref '{data_ref}' points outside storage directory {base}"
        )

    return candidate


# ============================================================================
# Репозиторий вычислений и результатов (ToolResult & data_ref)
# ============================================================================


def _get_dashboard_result_ids_for_dataset(
    db: Session, owner_id: str, dataset_id: str
) -> set[str]:
    """Возвращает множество всех result_id (включая шаги-предки) для дашбордов датасета."""
    dashboards = (
        db.query(DashboardModel)
        .filter(
            DashboardModel.owner_id == owner_id,
            DashboardModel.dataset_id == dataset_id,
        )
        .all()
    )
    valid_ids: set[str] = set()
    for dash in dashboards:
        for w in (dash.widgets or []):
            if not isinstance(w, dict):
                continue
            candidates: list[str] = []
            wid_res = w.get("result_id")
            if wid_res:
                candidates.append(wid_res)
            chart_res = (w.get("chart") or {}).get("result_id")
            if chart_res and chart_res not in candidates:
                candidates.append(chart_res)

            for cand_id in candidates:
                if cand_id in valid_ids:
                    continue
                curr_id: str | None = cand_id
                seen_ancestors: set[str] = set()
                while curr_id and curr_id not in seen_ancestors:
                    seen_ancestors.add(curr_id)
                    valid_ids.add(curr_id)
                    r = db.get(ToolResultModel, curr_id)
                    if r is None or r.owner_id != owner_id:
                        break
                    curr_id = r.source_result_id
    return valid_ids


def get_owned_tool_result(
    db: Session,
    result_id: str,
    owner_id: str,
    *,
    chat_id: str | None = None,
    dataset_id: str | None = None,
) -> ToolResultModel | None:
    """Validate stored ownership and the full provenance chain before any file read."""
    record = db.get(ToolResultModel, result_id)
    current = record
    seen: set[str] = set()
    expected_chat = chat_id if chat_id is not None else (record.chat_id if record else None)
    valid_dashboard_result_ids: set[str] | None = None

    while current is not None:
        if current.owner_id != owner_id or current.result_id in seen:
            return None
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", current.result_id) or (
            current.data_ref is not None
            and current.data_ref != f"parquet://{current.result_id}.parquet"
        ):
            return None
        seen.add(current.result_id)
        if expected_chat is not None and current.chat_id != expected_chat:
            return None
        if current.chat_id is not None:
            chat = db.get(ChatModel, current.chat_id)
            dataset = db.get(DatasetModel, chat.dataset_id) if chat else None
            if (
                chat is None
                or chat.owner_id != owner_id
                or dataset is None
                or dataset.owner_id != owner_id
                or (dataset_id is not None and dataset.dataset_id != dataset_id)
            ):
                return None
        elif dataset_id is not None:
            if valid_dashboard_result_ids is None:
                valid_dashboard_result_ids = _get_dashboard_result_ids_for_dataset(
                    db, owner_id=owner_id, dataset_id=dataset_id
                )
            if current.result_id not in valid_dashboard_result_ids:
                return None
        if not current.source_result_id:
            return record
        current = db.get(ToolResultModel, current.source_result_id)
    return None


def save_tool_result(
    db: Session,
    tool_result: ToolResult,
    chat_id: str | None = None,
    owner_id: str | None = None,
    persist_data: bool = True,
    storage_dir: Path | str | None = None,
) -> ToolResultModel:
    """Сохраняет вызов аналитического инструмента и его результат в БД (§4.3, B5-2).

    Сохраняет любые статусы: 'ok', 'error', 'empty'.
    Поддерживает сохранение Decimal, date, datetime в preview без ошибок сериализации.
    Табличные данные сохраняются в Parquet, формируя переносимую ссылку data_ref.
    """
    settings = get_settings()
    target_storage = Path(storage_dir or settings.results_data_dir).resolve()

    record = db.query(ToolResultModel).filter_by(result_id=tool_result.result_id).first()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool_result.result_id):
        raise ValueError("Некорректный идентификатор результата")

    # Разрешаем owner_id из чата, если не передан явно, и проверяем согласованность
    effective_owner_id = owner_id
    if chat_id is not None:
        chat = db.query(ChatModel).filter_by(chat_id=chat_id).first()
        if chat is None:
            raise ValueError("Чат результата не найден")
        if chat is not None:
            dataset = db.get(DatasetModel, chat.dataset_id)
            if dataset is None or dataset.owner_id != chat.owner_id:
                raise ValueError("Конфликт владельцев чата и датасета")
            if owner_id is not None and chat.owner_id is not None and owner_id != chat.owner_id:
                raise ValueError(
                    f"Конфликт владельцев: переданный owner_id='{owner_id}' "
                    f"не совпадает с владельцем чата chat.owner_id='{chat.owner_id}'"
                )
            if effective_owner_id is None:
                effective_owner_id = chat.owner_id

    # При upsert проверяем согласованность с уже сохранёнными связями
    if record is not None:
        if owner_id is not None and record.owner_id != effective_owner_id:
            raise ValueError("Конфликт владельцев: присвоение существующего результата запрещено")
        if chat_id is not None and record.chat_id != chat_id:
            raise ValueError("Конфликт владельцев: смена чата результата запрещена")
        # Проверяем владельца относительно уже сохранённого чата
        if record.chat_id is not None:
            saved_chat = db.query(ChatModel).filter_by(chat_id=record.chat_id).first()
            if saved_chat is not None:
                if (
                    effective_owner_id is not None
                    and saved_chat.owner_id is not None
                    and effective_owner_id != saved_chat.owner_id
                ):
                    raise ValueError(
                        f"Конфликт владельцев: переданный owner_id='{effective_owner_id}' "
                        f"не совпадает с владельцем сохранённого чата "
                        f"chat.owner_id='{saved_chat.owner_id}'"
                    )
                if chat_id is not None and chat_id != record.chat_id:
                    raise ValueError(
                        f"Конфликт владельцев: результат '{tool_result.result_id}' "
                        f"уже привязан к чату '{record.chat_id}', "
                        f"смена привязки к чату '{chat_id}' запрещена"
                    )
                if effective_owner_id is None:
                    effective_owner_id = saved_chat.owner_id

        # Проверяем неизменность уже сохранённого владельца
        if record.owner_id is not None:
            if effective_owner_id is not None and effective_owner_id != record.owner_id:
                raise ValueError(
                    f"Конфликт владельцев: результат уже принадлежит пользователю "
                    f"'{record.owner_id}', невозможно сменить владельца на '{effective_owner_id}'"
                )
            if effective_owner_id is None:
                effective_owner_id = record.owner_id

    effective_chat_id = chat_id or (record.chat_id if record else None)
    if tool_result.source_result_id and effective_owner_id is not None:
        if (
            tool_result.source_result_id == tool_result.result_id
            or get_owned_tool_result(
                db, tool_result.source_result_id, effective_owner_id, chat_id=effective_chat_id
            )
            is None
        ):
            raise ValueError("Источник результата не относится к владельцу и чату")
        parent = db.get(ToolResultModel, tool_result.source_result_id)
        while parent is not None:
            if parent.result_id == tool_result.result_id:
                raise ValueError("Циклическое происхождение результата запрещено")
            parent = (
                db.get(ToolResultModel, parent.source_result_id)
                if parent.source_result_id
                else None
            )

    data_ref = tool_result.data_ref
    if data_ref is not None:
        _resolve_data_ref_path(data_ref, target_storage, tool_result.result_id)
    data_preview: dict[str, Any] | None = None
    pending_parquet = None

    if tool_result.data is not None:
        columns = _normalize_columns(tool_result.data.columns)
        rows = tool_result.data.rows

        # Сохраняем в Parquet только если есть колонки и persist_data=True
        if persist_data and rows is not None and len(columns) > 0:
            file_name = f"{tool_result.result_id}.parquet"
            parquet_path = _resolve_data_ref_path(
                f"parquet://{file_name}", target_storage, tool_result.result_id
            )
            pending_parquet = (parquet_path, columns, rows, tool_result.data.columns)
            data_ref = f"parquet://{file_name}"

        preview_rows = rows[:50] if rows is not None else []
        columns_meta_dump = [
            c.model_dump() if hasattr(c, "model_dump") else c for c in tool_result.data.columns
        ]
        raw_preview = {
            "columns": columns,
            "columns_meta": columns_meta_dump,
            "rows": preview_rows,
            "row_count": tool_result.data.row_count
            if tool_result.data.row_count is not None
            else len(rows or []),
            "truncated": bool(tool_result.data.truncated),
        }
        # Санитизируем для JSON (Decimal -> str, date/datetime -> isoformat)
        data_preview = _sanitize_for_json(raw_preview)

    if (
        data_ref is not None
        and pending_parquet is None
        and (record is None or data_ref != record.data_ref)
    ):
        # A failed transaction or process crash may leave an unclaimed file.
        # Only a new materialization can bind it to a new result/owner.
        raise ValueError("Нельзя присвоить файл без сохранённого результата владельца")

    facts_data = (
        _sanitize_for_json([f.model_dump() for f in tool_result.facts])
        if tool_result.facts
        else None
    )
    chart_hint_data = (
        _sanitize_for_json(tool_result.chart_hint.model_dump())
        if tool_result.chart_hint is not None
        else None
    )
    error_data = (
        _sanitize_for_json(tool_result.error.model_dump())
        if tool_result.error is not None
        else None
    )
    params_data = _sanitize_for_json(tool_result.params) if tool_result.params else {}
    metric_data = (
        tool_result.metric.model_dump(mode="json") if tool_result.metric is not None else None
    )
    quality_data = (
        tool_result.quality.model_dump(mode="json") if tool_result.quality is not None else None
    )
    read_data = tool_result.read.model_dump(mode="json") if tool_result.read is not None else None

    if record is None:
        record = ToolResultModel(
            result_id=tool_result.result_id,
            owner_id=effective_owner_id,
            chat_id=chat_id,
            tool=tool_result.tool,
            params=params_data,
            sql=tool_result.sql,
            status=tool_result.status,
            facts=facts_data,
            chart_hint=chart_hint_data,
            error=error_data,
            data_ref=data_ref,
            data_preview=data_preview,
            source_result_id=tool_result.source_result_id,
            duration_ms=tool_result.duration_ms,
            metric=metric_data,
            quality=quality_data,
            read=read_data,
        )
        db.add(record)
    else:
        if effective_owner_id is not None:
            record.owner_id = effective_owner_id
        if chat_id is not None:
            record.chat_id = chat_id
        record.tool = tool_result.tool
        record.params = params_data
        record.sql = tool_result.sql
        record.status = tool_result.status
        record.facts = facts_data
        record.chart_hint = chart_hint_data
        record.error = error_data
        record.data_ref = data_ref
        record.data_preview = data_preview
        record.source_result_id = tool_result.source_result_id
        record.duration_ms = tool_result.duration_ms
        record.metric = metric_data
        record.quality = quality_data
        if read_data is not None:  # what a stored result once read is never erased
            record.read = read_data

    try:
        # The unique DB key claims this ID/owner before any shared file is touched.
        # A concurrent INSERT waits here and loses without replacing the winner's
        # Parquet. Keep the claim uncommitted until its atomic file write succeeds.
        db.flush()
        if pending_parquet is not None:
            write_parquet(*pending_parquet)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Не удалось сохранить результат: конфликт записей") from None
    except Exception:
        db.rollback()
        raise
    db.refresh(record)
    return record


def load_tool_result(
    db: Session,
    result_id: str,
    load_data: bool = True,
    storage_dir: Path | str | None = None,
    *,
    owner_id: str | None = None,
    chat_id: str | None = None,
    dataset_id: str | None = None,
) -> ToolResult | None:
    """Восстанавливает контракт ToolResult из БД и Parquet по ссылке data_ref."""
    record = (
        get_owned_tool_result(db, result_id, owner_id, chat_id=chat_id, dataset_id=dataset_id)
        if owner_id is not None
        else db.get(ToolResultModel, result_id)
    )
    if record is None:
        return None

    data: ToolResultData | None = None

    if load_data:
        columns: list[str | ToolResultColumn] = []
        row_count: int | None = None
        truncated: bool = False

        if record.data_preview:
            if "columns_meta" in record.data_preview:
                columns = [
                    ToolResultColumn(**c) if isinstance(c, dict) and "name" in c else c
                    for c in record.data_preview["columns_meta"]
                ]
            elif "columns" in record.data_preview:
                columns = list(record.data_preview["columns"])
            row_count = record.data_preview.get("row_count")
            truncated = record.data_preview.get("truncated", False)

        if record.data_ref:
            try:
                parquet_path = _resolve_data_ref_path(
                    record.data_ref, storage_dir, expected_result_id=result_id
                )
                if parquet_path.exists():
                    try:
                        cols, rows = read_parquet(parquet_path)
                        if not columns:
                            columns = cols
                        if row_count is None:
                            row_count = len(rows)
                        data = ToolResultData(
                            columns=columns,
                            rows=rows,
                            row_count=row_count,
                            truncated=truncated,
                        )
                    except (duckdb.Error, OSError) as exc:
                        logger.warning(
                            "Ошибка чтения Parquet для результата %s: %s, берём превью",
                            result_id,
                            exc,
                        )
                        # При повреждении Parquet на диске откатываемся к сохранённому preview
                        if record.data_preview and "rows" in record.data_preview:
                            p_rows = record.data_preview["rows"]
                            eff_rc = row_count if row_count is not None else len(p_rows)
                            data = ToolResultData(
                                columns=columns,
                                rows=p_rows,
                                row_count=eff_rc,
                                truncated=True,
                            )
                elif record.data_preview and "rows" in record.data_preview:
                    p_rows = record.data_preview["rows"]
                    eff_rc = row_count if row_count is not None else len(p_rows)
                    data = ToolResultData(
                        columns=columns,
                        rows=p_rows,
                        row_count=eff_rc,
                        truncated=truncated or (eff_rc > len(p_rows)),
                    )
            except ValueError as exc:
                if owner_id is not None:
                    return None
                logger.warning(
                    "Некорректный data_ref '%s' для результата %s: %s",
                    record.data_ref,
                    result_id,
                    exc,
                )
                # При некорректном data_ref нейтрализуем ошибку и используем preview
                if record.data_preview and "rows" in record.data_preview:
                    p_rows = record.data_preview["rows"]
                    eff_rc = row_count if row_count is not None else len(p_rows)
                    data = ToolResultData(
                        columns=columns,
                        rows=p_rows,
                        row_count=eff_rc,
                        truncated=True,
                    )
        elif record.data_preview and "rows" in record.data_preview:
            p_rows = record.data_preview["rows"]
            eff_rc = row_count if row_count is not None else len(p_rows)
            data = ToolResultData(
                columns=columns,
                rows=p_rows,
                row_count=eff_rc,
                truncated=truncated or (eff_rc > len(p_rows)),
            )

    facts = [Fact(**f) for f in (record.facts or [])]
    chart_hint = ChartHint(**record.chart_hint) if record.chart_hint else None
    error = ToolError(**record.error) if record.error else None

    return ToolResult(
        result_id=record.result_id,
        status=record.status,  # type: ignore[arg-type]
        data=data,
        data_ref=record.data_ref,
        facts=facts,
        chart_hint=chart_hint,
        error=error,
        tool=record.tool,
        params=record.params or {},
        sql=record.sql,
        source_result_id=record.source_result_id,
        duration_ms=record.duration_ms,
        metric=MetricProvenance.model_validate(record.metric) if record.metric else None,
        quality=QualityReport.model_validate(record.quality) if record.quality else None,
        read=ReadProvenance.model_validate(record.read)
        if record.read
        else ReadProvenance.unknown(),
    )


# ============================================================================
# Репозиторий пользователей, датасетов, чатов, сообщений и дашбордов
# ============================================================================


def get_or_create_user(
    db: Session,
    user_id: str,
    username: str,
    email: str | None = None,
    password_hash: str | None = None,
) -> UserModel:
    """Находит или создает пользователя (владельца ресурсов)."""
    user = db.query(UserModel).filter_by(id=user_id).first()
    if user is None:
        user = UserModel(
            id=user_id,
            username=username,
            email=email,
            password_hash=password_hash,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def create_dataset(
    db: Session,
    dataset_id: str,
    owner_id: str,
    name: str,
    source_kind: str = "csv",
    catalog: dict[str, Any] | None = None,
    connection: str | None = None,
    status: str = "ready",
) -> DatasetModel:
    """Создает запись о датасете, привязанном к владельцу."""
    dataset = DatasetModel(
        dataset_id=dataset_id,
        owner_id=owner_id,
        name=name,
        source_kind=source_kind,
        catalog=catalog,
        connection=connection,
        status=status,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def get_dataset(db: Session, dataset_id: str) -> DatasetModel | None:
    """Возвращает датасет по его идентификатору."""
    return db.query(DatasetModel).filter_by(dataset_id=dataset_id).first()


def list_datasets(db: Session, owner_id: str | None = None) -> list[DatasetModel]:
    """Возвращает список датасетов (с фильтрацией по владельцу при необходимости)."""
    stmt = select(DatasetModel)
    if owner_id is not None:
        stmt = stmt.where(DatasetModel.owner_id == owner_id)
    return list(db.scalars(stmt).all())


def create_chat(
    db: Session,
    chat_id: str,
    owner_id: str,
    dataset_id: str,
    title: str = "Новый чат",
    summary: str | None = None,
    focus_result_ids: list[str] | None = None,
) -> ChatModel:
    """Создает новый чат диалога, привязанный к пользователю и датасету."""
    chat = ChatModel(
        chat_id=chat_id,
        owner_id=owner_id,
        dataset_id=dataset_id,
        title=title,
        summary=summary,
        focus_result_ids=focus_result_ids or [],
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


def get_chat(db: Session, chat_id: str) -> ChatModel | None:
    """Возвращает чат по его идентификатору."""
    return db.query(ChatModel).filter_by(chat_id=chat_id).first()


def update_chat_memory(
    db: Session,
    chat_id: str,
    *,
    summary: str | None = None,
    focus_result_ids: list[str] | None = None,
) -> ChatModel | None:
    """Сохраняет summary и текущий фокус разговора, не меняя соседние чаты."""
    chat = get_chat(db, chat_id)
    if chat is None:
        return None
    if summary is not None:
        chat.summary = summary
    if focus_result_ids is not None:
        chat.focus_result_ids = list(
            dict.fromkeys(result_id for result_id in focus_result_ids if result_id)
        )
    db.commit()
    db.refresh(chat)
    return chat


def list_chats(
    db: Session, owner_id: str | None = None, dataset_id: str | None = None
) -> list[ChatModel]:
    """Возвращает чаты с опциональной фильтрацией, сначала новые."""
    stmt = select(ChatModel)
    if owner_id is not None:
        stmt = stmt.where(ChatModel.owner_id == owner_id)
    if dataset_id is not None:
        stmt = stmt.where(ChatModel.dataset_id == dataset_id)
    return list(db.scalars(stmt.order_by(ChatModel.created_at.desc())).all())


def save_message(
    db: Session,
    message_id: str,
    chat_id: str,
    role: str,
    content: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    result_ids: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
    turn: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    before_commit: Callable[[], None] | None = None,
) -> MessageModel:
    """Сохраняет сообщение в чате со структурированными блоками и аудитом."""
    effective_created_at = created_at
    if effective_created_at is not None:
        if effective_created_at.tzinfo is None:
            effective_created_at = effective_created_at.replace(tzinfo=UTC)
        else:
            effective_created_at = effective_created_at.astimezone(UTC)
    else:
        effective_created_at = _utc_now()

    msg = MessageModel(
        message_id=message_id,
        chat_id=chat_id,
        role=role,
        content=content,
        blocks=blocks,
        result_ids=result_ids,
        trace=trace,
        turn=turn,
        created_at=effective_created_at,
    )
    db.add(msg)
    try:
        if before_commit is not None:
            db.flush()
            before_commit()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if before_commit is None:
        db.refresh(msg)
    return msg


def save_assistant_message(
    db: Session,
    message: AssistantMessage,
    *,
    budget: ExecutionBudget | None = None,
    question_id: str | None = None,
    completed_turn: ChatTurnState | None = None,
    also: Callable[[Session], None] | None = None,
    summary: str | None = None,
    focus_result_ids: list[str] | None = None,
) -> MessageModel:
    """Сохраняет контракт AssistantMessage в базу данных с сохранением времени created_at.

    ``summary`` и ``focus_result_ids`` обновляются в той же транзакции, что и
    сообщение с терминальным статусом хода. При исчерпании бюджета или
    проигранном CAS откатывается всё вместе: память не опережает ответ.
    ``also`` добавляет связанные записи (например, кандидатов метрик) в ту же транзакцию.
    """
    blocks_data = [b.model_dump() for b in message.blocks]
    trace_data = [t.model_dump() for t in message.trace]

    def write(session: Session) -> MessageModel:
        if question_id is not None and completed_turn is not None:
            if not apply_turn(session, question_id, completed_turn):
                session.rollback()
                raise TurnCancelled
        if summary is not None or focus_result_ids is not None:
            chat = session.get(ChatModel, message.chat_id)
            if chat is None:
                raise ValueError("Чат результата не найден")
            if summary is not None:
                chat.summary = summary
            if focus_result_ids is not None:
                chat.focus_result_ids = list(
                    dict.fromkeys(result_id for result_id in focus_result_ids if result_id)
                )

        def before_commit() -> None:
            if also is not None:
                also(session)
            if budget is not None:
                budget.check()

        return save_message(
            db=session,
            message_id=message.message_id,
            chat_id=message.chat_id,
            role=message.role,
            content=None,
            blocks=blocks_data,
            result_ids=message.result_ids,
            trace=trace_data,
            created_at=message.created_at,
            before_commit=before_commit if also is not None or budget is not None else None,
        )

    return budgeted_write(db, budget, write) if budget is not None else write(db)


def to_assistant_message(message_model: MessageModel) -> AssistantMessage:
    """Восстанавливает контракт AssistantMessage из MessageModel с сохранением момента времени."""
    created_at = message_model.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)

    return AssistantMessage(
        message_id=message_model.message_id,
        chat_id=message_model.chat_id,
        role="assistant",
        created_at=created_at,
        blocks=message_model.blocks or [],
        result_ids=message_model.result_ids or [],
        trace=message_model.trace or [],
    )


def load_assistant_message(db: Session, message_id: str) -> AssistantMessage | None:
    """Загружает сообщение ассистента по message_id и восстанавливает контракт AssistantMessage."""
    record = db.query(MessageModel).filter_by(message_id=message_id).first()
    if record is None or record.role != "assistant":
        return None
    return to_assistant_message(record)


def get_chat_messages(db: Session, chat_id: str) -> list[MessageModel]:
    """Возвращает хронологический список сообщений чата."""
    stmt = (
        select(MessageModel)
        .where(MessageModel.chat_id == chat_id)
        .order_by(MessageModel.created_at.asc())
    )
    return list(db.scalars(stmt).all())


def create_dashboard(
    db: Session,
    dashboard_id: str,
    owner_id: str,
    dataset_id: str,
    title: str,
    description: str | None = None,
    widgets: list[dict[str, Any]] | None = None,
    source_chat_id: str | None = None,
) -> DashboardModel:
    """Создает дашборд со списком виджетов."""
    dashboard = DashboardModel(
        dashboard_id=dashboard_id,
        owner_id=owner_id,
        dataset_id=dataset_id,
        title=title,
        description=description,
        widgets=widgets or [],
        source_chat_id=source_chat_id,
    )
    db.add(dashboard)
    db.commit()
    db.refresh(dashboard)
    return dashboard


def get_dashboard(
    db: Session, dashboard_id: str, owner_id: str | None = None
) -> DashboardModel | None:
    """Возвращает дашборд по идентификатору и опциональному владельцу."""
    stmt = select(DashboardModel).where(DashboardModel.dashboard_id == dashboard_id)
    if owner_id is not None:
        stmt = stmt.where(DashboardModel.owner_id == owner_id)
    return db.scalars(stmt).first()


def update_dashboard(
    db: Session,
    dashboard_id: str,
    *,
    owner_id: str,
    title: str | None = None,
    description: str | None = None,
    widgets: list[dict[str, Any]] | None = None,
    refreshed_at: datetime | None = None,
) -> DashboardModel | None:
    """Обновляет метаданные, виджеты или время пересчёта дашборда."""
    dashboard = get_dashboard(db, dashboard_id, owner_id=owner_id)
    if dashboard is None:
        return None
    if title is not None:
        dashboard.title = title
    if description is not None:
        dashboard.description = description
    if widgets is not None:
        dashboard.widgets = copy.deepcopy(widgets)
        flag_modified(dashboard, "widgets")
    if refreshed_at is not None:
        dashboard.refreshed_at = refreshed_at
    db.commit()
    db.refresh(dashboard)
    return dashboard


def list_dashboards(
    db: Session, owner_id: str | None = None, dataset_id: str | None = None
) -> list[DashboardModel]:
    """Возвращает список дашбордов."""
    stmt = select(DashboardModel)
    if owner_id is not None:
        stmt = stmt.where(DashboardModel.owner_id == owner_id)
    if dataset_id is not None:
        stmt = stmt.where(DashboardModel.dataset_id == dataset_id)
    return list(db.scalars(stmt).all())
