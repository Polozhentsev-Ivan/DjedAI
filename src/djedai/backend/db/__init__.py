"""Модуль работы с базой данных приложения DjedAI."""

from __future__ import annotations

from djedai.backend.db.base import Base
from djedai.backend.db.models import (
    ChatModel,
    DashboardModel,
    DatasetModel,
    MessageModel,
    ToolResultModel,
    UserModel,
)
from djedai.backend.db.repositories import (
    create_chat,
    create_dashboard,
    create_dataset,
    get_chat,
    get_chat_messages,
    get_dashboard,
    get_dataset,
    get_or_create_user,
    list_chats,
    list_dashboards,
    list_datasets,
    load_tool_result,
    read_parquet,
    save_assistant_message,
    save_message,
    save_tool_result,
    write_parquet,
)
from djedai.backend.db.session import create_db_engine, get_db, get_engine, init_db

__all__ = [
    "Base",
    "ChatModel",
    "DashboardModel",
    "DatasetModel",
    "MessageModel",
    "ToolResultModel",
    "UserModel",
    "create_chat",
    "create_dashboard",
    "create_dataset",
    "create_db_engine",
    "get_chat",
    "get_chat_messages",
    "get_dashboard",
    "get_dataset",
    "get_db",
    "get_engine",
    "get_or_create_user",
    "init_db",
    "list_chats",
    "list_dashboards",
    "list_datasets",
    "load_tool_result",
    "read_parquet",
    "save_assistant_message",
    "save_message",
    "save_tool_result",
    "write_parquet",
]
