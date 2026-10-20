"""Эндпоинт проверки доступности сервиса (Health Check)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from djedai.backend.config import Settings, get_settings
from djedai.contracts.common import HealthCheck

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthCheck)
def get_health(request: Request) -> HealthCheck:
    """Возвращает статус работоспособности, окружение и версию бэкенда."""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return HealthCheck(
        status="ok",
        app_env=settings.app_env,
        version=settings.app_version,
    )
