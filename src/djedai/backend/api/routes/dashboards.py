"""Роутер дашбордов (§7.2, B5-6)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from djedai.backend.auth.dependencies import current_owner_id, get_current_user
from djedai.backend.config import get_settings
from djedai.backend.dashboard_service import DashboardService
from djedai.backend.db.session import get_db
from djedai.contracts.dashboard import (
    DashboardCreate,
    DashboardPatch,
    DashboardSnapshot,
    DashboardSummary,
)

router = APIRouter(
    prefix="/dashboards", tags=["dashboards"], dependencies=[Depends(get_current_user)]
)
Db = Annotated[Session, Depends(get_db)]
Owner = Annotated[str, Depends(current_owner_id)]


def _get_dashboard_service(request: Request) -> DashboardService:
    service = getattr(request.app.state, "dashboard_service", None)
    if service is not None:
        return service
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return DashboardService(settings)


@router.get("", response_model=list[DashboardSummary])
def list_dashboards(
    dataset_id: str, request: Request, db: Db, owner_id: Owner
) -> list[DashboardSummary]:
    """Возвращает список дашбордов владельца по выбранному датасету."""
    service = _get_dashboard_service(request)
    return service.list_dashboards(db, owner_id, dataset_id)


@router.post("", response_model=DashboardSnapshot)
def create_dashboard(
    dashboard: DashboardCreate, request: Request, db: Db, owner_id: Owner
) -> DashboardSnapshot:
    """Создаёт новый дашборд или добавляет виджет в существующий."""
    service = _get_dashboard_service(request)
    return service.create_or_add_widget(db, owner_id, dashboard)


@router.get("/{dashboard_id}", response_model=DashboardSnapshot)
def get_dashboard(
    dashboard_id: str, request: Request, db: Db, owner_id: Owner
) -> DashboardSnapshot:
    """Возвращает снимок дашборда по идентификатору без запуска пересчёта."""
    service = _get_dashboard_service(request)
    return service.get_dashboard(db, owner_id, dashboard_id)


@router.post("/{dashboard_id}/refresh", response_model=DashboardSnapshot)
def refresh_dashboard(
    dashboard_id: str, request: Request, db: Db, owner_id: Owner
) -> DashboardSnapshot:
    """Перевыполняет виджеты дашборда автономно без активного чата."""
    service = _get_dashboard_service(request)
    dataset_service = getattr(request.app.state, "dataset_service", None)
    metric_service = getattr(request.app.state, "metric_service", None)
    if dataset_service is None or metric_service is None:
        raise HTTPException(503, "Сервисы данных временно недоступны")
    return service.refresh_dashboard(db, dataset_service, metric_service, owner_id, dashboard_id)


@router.patch("/{dashboard_id}", response_model=DashboardSnapshot)
def update_dashboard(
    dashboard_id: str, patch: DashboardPatch, request: Request, db: Db, owner_id: Owner
) -> DashboardSnapshot:
    """Обновляет название, удаляет виджеты или перестраивает сетку дашборда."""
    service = _get_dashboard_service(request)
    return service.patch_dashboard(db, owner_id, dashboard_id, patch)
