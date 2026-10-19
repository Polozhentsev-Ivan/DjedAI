"""Репозитории и сервисы хранения данных приложения (§7.1, B5-2).

Обеспечивают:
- Сохранение и чтение вызовов инструментов (ToolResult со статусами ok, error, empty)
- Хранение больших данных по data_ref в формате Parquet через DuckDB
- Сохранение и чтение пользователей, датасетов, чатов, сообщений и дашбордов
- Поддержание связей владения (owner_id) между сущностями
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from djedai.backend.config import get_settings
from djedai.backend.db.models import (
    ChatModel,
    DashboardModel,
    DatasetModel,
    MessageModel,
    ToolResultModel,
    UserModel,
)
from djedai.contracts.message import AssistantMessage
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultData,
)


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


def write_parquet(file_path: Path | str, columns: list[str], rows: list[list[Any]]) -> Path:
    """Записывает колонки и строки в Parquet-файл через встроенный движок DuckDB."""
    path = Path(file_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    duck_path = path.as_posix()

    con = duckdb.connect()
    try:
        if not rows:
            col_defs = ", ".join(f'"{col}" VARCHAR' for col in columns)
            con.execute(f"CREATE TEMPORARY TABLE _res_empty ({col_defs})")
            con.execute(f"COPY _res_empty TO '{duck_path}' (FORMAT PARQUET)")
            return path

        col_defs = []
        for i, col in enumerate(columns):
            sample_val = None
            for r in rows:
                if i < len(r) and r[i] is not None:
                    sample_val = r[i]
                    break
            if isinstance(sample_val, bool):
                t = "BOOLEAN"
            elif isinstance(sample_val, int):
                t = "BIGINT"
            elif isinstance(sample_val, float):
                t = "DOUBLE"
            else:
                t = "VARCHAR"
            col_defs.append(f'"{col}" {t}')

        placeholders = ", ".join(["?"] * len(columns))
        con.execute(f"CREATE TEMPORARY TABLE _res_data ({', '.join(col_defs)})")

        formatted_rows = [
            [
                r[i]
                if i < len(r) and not isinstance(r[i], (dict, list))
                else (str(r[i]) if i < len(r) and r[i] is not None else None)
                for i in range(len(columns))
            ]
            for r in rows
        ]
        con.executemany(f"INSERT INTO _res_data VALUES ({placeholders})", formatted_rows)
        con.execute(f"COPY _res_data TO '{duck_path}' (FORMAT PARQUET)")
        return path
    finally:
        con.close()


def read_parquet(file_path: Path | str) -> tuple[list[str], list[list[Any]]]:
    """Читает колонки и строки из Parquet-файла через DuckDB."""
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Parquet-файл не найден: {path}")

    duck_path = path.as_posix()
    con = duckdb.connect()
    try:
        res = con.execute(f"SELECT * FROM '{duck_path}'").fetchall()
        cols = [desc[0] for desc in con.description]
        rows = [list(r) for r in res]
        return cols, rows
    finally:
        con.close()


def _resolve_data_ref_path(data_ref: str, base_dir: Path | str | None = None) -> Path:
    """Разрешает путь к файлу данных из data_ref ссылки."""
    ref = data_ref
    for prefix in ("parquet://", "file://"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
            break

    p = Path(ref)
    if p.is_absolute():
        return p

    base = Path(base_dir or get_settings().results_data_dir)
    return (base / p).resolve()


# ============================================================================
# Репозиторий вычислений и результатов (ToolResult & data_ref)
# ============================================================================


def save_tool_result(
    db: Session,
    tool_result: ToolResult,
    chat_id: str | None = None,
    persist_data: bool = True,
    storage_dir: Path | str | None = None,
) -> ToolResultModel:
    """Сохраняет вызов аналитического инструмента и его результат в БД (§4.3, B5-2).

    Сохраняет любые статусы: 'ok', 'error', 'empty'.
    Если присутствуют табличные данные и persist_data=True, сохраняет их в Parquet
    и формирует ссылку data_ref.
    """
    settings = get_settings()
    target_storage = Path(storage_dir or settings.results_data_dir)

    data_ref = tool_result.data_ref
    data_preview: dict[str, Any] | None = None

    if tool_result.data is not None:
        columns = _normalize_columns(tool_result.data.columns)
        rows = tool_result.data.rows

        if persist_data and rows is not None:
            # Сохраняем Parquet на диск
            file_name = f"{tool_result.result_id}.parquet"
            parquet_path = target_storage / file_name
            write_parquet(parquet_path, columns, rows)
            data_ref = f"parquet://{parquet_path.as_posix()}"

        # Сохраняем компактное превью (до 50 строк) для быстрого рендера без чтения с диска
        preview_rows = rows[:50] if rows is not None else []
        data_preview = {
            "columns": columns,
            "rows": preview_rows,
            "row_count": tool_result.data.row_count
            if tool_result.data.row_count is not None
            else len(rows or []),
            "truncated": tool_result.data.truncated or (len(rows or []) > 50),
        }

    # Сериализация сопутствующих структур
    facts_data = [f.model_dump() for f in tool_result.facts] if tool_result.facts else None
    chart_hint_data = (
        tool_result.chart_hint.model_dump() if tool_result.chart_hint is not None else None
    )
    error_data = tool_result.error.model_dump() if tool_result.error is not None else None

    # Поиск существующей записи для обновления или создание новой
    record = db.query(ToolResultModel).filter_by(result_id=tool_result.result_id).first()
    if record is None:
        record = ToolResultModel(
            result_id=tool_result.result_id,
            chat_id=chat_id,
            tool=tool_result.tool,
            params=tool_result.params,
            sql=tool_result.sql,
            status=tool_result.status,
            facts=facts_data,
            chart_hint=chart_hint_data,
            error=error_data,
            data_ref=data_ref,
            data_preview=data_preview,
            source_result_id=tool_result.source_result_id,
            duration_ms=tool_result.duration_ms,
        )
        db.add(record)
    else:
        if chat_id is not None:
            record.chat_id = chat_id
        record.tool = tool_result.tool
        record.params = tool_result.params
        record.sql = tool_result.sql
        record.status = tool_result.status
        record.facts = facts_data
        record.chart_hint = chart_hint_data
        record.error = error_data
        record.data_ref = data_ref
        record.data_preview = data_preview
        record.source_result_id = tool_result.source_result_id
        record.duration_ms = tool_result.duration_ms

    db.commit()
    db.refresh(record)
    return record


def load_tool_result(
    db: Session,
    result_id: str,
    load_data: bool = True,
    storage_dir: Path | str | None = None,
) -> ToolResult | None:
    """Восстанавливает контракт ToolResult из БД и Parquet по ссылке data_ref."""
    record = db.query(ToolResultModel).filter_by(result_id=result_id).first()
    if record is None:
        return None

    data: ToolResultData | None = None

    if load_data:
        if record.data_ref:
            try:
                parquet_path = _resolve_data_ref_path(record.data_ref, storage_dir)
                cols, rows = read_parquet(parquet_path)
                data = ToolResultData(
                    columns=cols,
                    rows=rows,
                    row_count=len(rows),
                    truncated=False,
                )
            except Exception:
                # Если файл Parquet временно недоступен, берем из preview
                if record.data_preview:
                    data = ToolResultData(**record.data_preview)
        elif record.data_preview:
            data = ToolResultData(**record.data_preview)

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
) -> ChatModel:
    """Создает новый чат диалога, привязанный к пользователю и датасету."""
    chat = ChatModel(
        chat_id=chat_id,
        owner_id=owner_id,
        dataset_id=dataset_id,
        title=title,
        summary=summary,
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


def get_chat(db: Session, chat_id: str) -> ChatModel | None:
    """Возвращает чат по его идентификатору."""
    return db.query(ChatModel).filter_by(chat_id=chat_id).first()


def list_chats(
    db: Session, owner_id: str | None = None, dataset_id: str | None = None
) -> list[ChatModel]:
    """Возвращает список чатов с опциональной фильтрацией."""
    stmt = select(ChatModel)
    if owner_id is not None:
        stmt = stmt.where(ChatModel.owner_id == owner_id)
    if dataset_id is not None:
        stmt = stmt.where(ChatModel.dataset_id == dataset_id)
    return list(db.scalars(stmt).all())


def save_message(
    db: Session,
    message_id: str,
    chat_id: str,
    role: str,
    content: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    result_ids: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> MessageModel:
    """Сохраняет сообщение в чате со структурированными блоками и аудитом."""
    msg = MessageModel(
        message_id=message_id,
        chat_id=chat_id,
        role=role,
        content=content,
        blocks=blocks,
        result_ids=result_ids,
        trace=trace,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def save_assistant_message(db: Session, message: AssistantMessage) -> MessageModel:
    """Сохраняет контракт AssistantMessage в базу данных."""
    blocks_data = [b.model_dump() for b in message.blocks]
    trace_data = [t.model_dump() for t in message.trace]
    return save_message(
        db=db,
        message_id=message.message_id,
        chat_id=message.chat_id,
        role=message.role,
        content=None,
        blocks=blocks_data,
        result_ids=message.result_ids,
        trace=trace_data,
    )


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


def get_dashboard(db: Session, dashboard_id: str) -> DashboardModel | None:
    """Возвращает дашборд по идентификатору."""
    return db.query(DashboardModel).filter_by(dashboard_id=dashboard_id).first()


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
