"""Базовый декларативный класс для моделей БД приложения."""

from __future__ import annotations

from sqlalchemy.orm import declarative_base

Base = declarative_base()
