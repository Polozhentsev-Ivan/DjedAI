"""Модуль работы с базой данных приложения DjedAI."""

from __future__ import annotations

from djedai.backend.db.base import Base
from djedai.backend.db.session import create_db_engine, get_db, get_engine, init_db

__all__ = ["Base", "create_db_engine", "get_db", "get_engine", "init_db"]
