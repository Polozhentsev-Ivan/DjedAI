"""SQLAlchemy модели сущностей приложения DjedAI (§7.1, B5-2).

Включает:
- users: пользователи и владельцы ресурсов (AUTH-1)
- datasets: метаданные датасетов, схема/каталог и статус
- chats: чаты диалогов с агентом
- messages: сообщения с блоками и ссылками на результаты
- results: аудит вызовов аналитических инструментов, параметры, SQL, статус и data_ref
- dashboards: дашборды с перевычисляемыми виджетами
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from djedai.backend.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UserModel(Base):
    """Модель пользователя приложения (владельца ресурсов)."""

    __tablename__ = "users"

    id = Column(String(64), primary_key=True, index=True)
    username = Column(String(128), unique=True, index=True, nullable=False)
    email = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    # Связи владения
    datasets = relationship("DatasetModel", back_populates="owner", cascade="all, delete-orphan")
    chats = relationship("ChatModel", back_populates="owner", cascade="all, delete-orphan")
    dashboards = relationship(
        "DashboardModel", back_populates="owner", cascade="all, delete-orphan"
    )

    @property
    def user_id(self) -> str:
        """Псевдоним для совместимости с UserRead/UserBase контрактами."""
        return self.id


class DatasetModel(Base):
    """Модель датасета (B1, B5)."""

    __tablename__ = "datasets"

    dataset_id = Column(String(64), primary_key=True, index=True)
    owner_id = Column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name = Column(String(255), nullable=False)
    source_kind = Column(String(64), nullable=False)  # csv, postgres, clickhouse
    connection = Column(Text, nullable=True)  # зашифрованная строка подключения
    catalog = Column(JSON, nullable=True)  # сериализованный DatasetCatalog
    status = Column(
        String(64), default="ready", nullable=False
    )  # uploading, profiling, ready, error
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    # Связи
    owner = relationship("UserModel", back_populates="datasets")
    chats = relationship("ChatModel", back_populates="dataset", cascade="all, delete-orphan")
    dashboards = relationship(
        "DashboardModel", back_populates="dataset", cascade="all, delete-orphan"
    )


class ChatModel(Base):
    """Модель диалогового чата с агентом (B3, B5)."""

    __tablename__ = "chats"

    chat_id = Column(String(64), primary_key=True, index=True)
    owner_id = Column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dataset_id = Column(
        String(64),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title = Column(String(255), nullable=False, default="Новый чат")
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    # Связи
    owner = relationship("UserModel", back_populates="chats")
    dataset = relationship("DatasetModel", back_populates="chats")
    messages = relationship(
        "MessageModel",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="MessageModel.created_at",
    )
    results = relationship("ToolResultModel", back_populates="chat")


class MessageModel(Base):
    """Модель сообщения чата со структурированными блоками и аудитом (B3, B5)."""

    __tablename__ = "messages"

    message_id = Column(String(64), primary_key=True, index=True)
    chat_id = Column(
        String(64), ForeignKey("chats.chat_id", ondelete="CASCADE"), index=True, nullable=False
    )
    role = Column(String(32), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=True)
    blocks = Column(JSON, nullable=True)  # список словарей MessageBlock
    result_ids = Column(JSON, nullable=True)  # список ID результатов (provenance)
    trace = Column(JSON, nullable=True)  # шаги выполнения графа
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)

    # Связи
    chat = relationship("ChatModel", back_populates="messages")


class ToolResultModel(Base):
    """Модель аудита вызова инструмента и результата вычислений (§4.3, §7.1)."""

    __tablename__ = "results"

    result_id = Column(String(64), primary_key=True, index=True)
    chat_id = Column(
        String(64), ForeignKey("chats.chat_id", ondelete="SET NULL"), index=True, nullable=True
    )
    tool = Column(String(128), nullable=True)
    params = Column(JSON, nullable=True)
    sql = Column(Text, nullable=True)
    status = Column(String(32), nullable=False)  # ok, error, empty
    facts = Column(JSON, nullable=True)
    chart_hint = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)  # ToolError {code, message, suggestion}
    data_ref = Column(String(512), nullable=True)  # ссылка на Parquet/DuckDB
    data_preview = Column(JSON, nullable=True)  # превью таблицы для небольших данных
    source_result_id = Column(String(64), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)

    # Связи
    chat = relationship("ChatModel", back_populates="results")


class DashboardModel(Base):
    """Модель дашборда с перевычисляемыми виджетами (§7.2, B5)."""

    __tablename__ = "dashboards"

    dashboard_id = Column(String(64), primary_key=True, index=True)
    owner_id = Column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dataset_id = Column(
        String(64),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    source_chat_id = Column(
        String(64), ForeignKey("chats.chat_id", ondelete="SET NULL"), nullable=True
    )
    widgets = Column(JSON, nullable=False, default=list)  # список DashboardWidget
    created_at = Column(DateTime(timezone=True), default=_utc_now, nullable=False)
    refreshed_at = Column(DateTime(timezone=True), nullable=True)

    # Связи
    owner = relationship("UserModel", back_populates="dashboards")
    dataset = relationship("DatasetModel", back_populates="dashboards")
    source_chat = relationship("ChatModel")
