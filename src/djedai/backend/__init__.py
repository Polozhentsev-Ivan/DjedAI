"""Бэкенд-сервис DjedAI: FastAPI, база данных приложения, миграции и API."""

from __future__ import annotations

from djedai.backend.app import app, create_app
from djedai.backend.config import Settings, get_settings

__all__ = ["Settings", "app", "create_app", "get_settings"]
