"""Подключение к БД приложения и управление сессиями SQLAlchemy."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.config import get_settings
from djedai.backend.db.base import Base
from djedai.execution import ExecutionBudget


def budgeted_write[T](db: Session, budget: ExecutionBudget, write: Callable[[Session], T]) -> T:
    """Retry SQLite lock contention cooperatively on a pinned writer connection.

    The caller must flush and check the budget before committing. A zero SQLite
    busy timeout also prevents COMMIT from waiting behind a long-lived reader.
    The original connection's timeout is restored before returning it to the pool.
    """
    engine = db.get_bind().engine
    if engine.dialect.name != "sqlite" or engine.url.database in {None, "", ":memory:"}:
        budget.check()
        return write(db)
    with engine.connect() as connection:
        driver = connection.connection.driver_connection
        original_timeout = driver.execute("PRAGMA busy_timeout").fetchone()[0]
        driver.execute("PRAGMA busy_timeout=0")
        try:
            with Session(bind=connection, expire_on_commit=False) as writer:
                while True:
                    budget.check()
                    try:
                        return write(writer)
                    except OperationalError as exc:
                        code = getattr(exc.orig, "sqlite_errorcode", 0)
                        if code & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                            raise
                        # Roll back before waiting, including a failed COMMIT.
                        # SQLAlchemy may already have deactivated that transaction.
                        driver.rollback()
                        writer.rollback()
                        budget.wait(0.05)
        finally:
            driver.execute(f"PRAGMA busy_timeout={original_timeout}")


def create_db_engine(database_url: str | None = None) -> Engine:
    """Создает Engine SQLAlchemy с поддержкой SQLite и PostgreSQL."""
    url = database_url or get_settings().app_database_url
    if not url:
        url = "sqlite:///./var/app.db"

    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Создаем каталог для SQLite файла, если путь файловый
        if ":///" in url and not url.startswith("sqlite:///:memory:"):
            file_path = url.split(":///", 1)[1]
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args=connect_args, echo=False)

    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Возвращает глобальный Engine приложения."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory(request: Request = None) -> sessionmaker[Session]:
    """Возвращает фабрику сессий."""
    if request is not None:
        return request.app.state.session_factory
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _session_factory


def get_db(request: Request = None) -> Generator[Session]:
    """Dependency для FastAPI роутеров: отдает сессию и закрывает ее после запроса."""
    session_factory = get_session_factory(request)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def init_db(engine: Engine | None = None) -> None:
    """Создает все таблицы в БД (для dev-режима и юнит-тестов)."""
    eng = engine or get_engine()
    Base.metadata.create_all(bind=eng)
