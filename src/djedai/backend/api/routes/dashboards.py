"""Роутер дашбордов (B5-6)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from djedai.backend.auth.dependencies import current_owner_id, get_current_user
from djedai.backend.db.models import DashboardModel, DatasetModel
from djedai.backend.db.repositories import get_owned_tool_result
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


def _require_dataset(db: Session, owner_id: str, dataset_id: str) -> None:
    dataset = db.get(DatasetModel, dataset_id)
    if dataset is None or dataset.owner_id != owner_id:
        raise HTTPException(404, "Датасет не найден")


def _require_dashboard(db: Session, owner_id: str, dashboard_id: str) -> None:
    dashboard = db.get(DashboardModel, dashboard_id)
    if dashboard is None or dashboard.owner_id != owner_id:
        raise HTTPException(404, "Дашборд не найден")
    _require_dataset(db, owner_id, dashboard.dataset_id)


@router.get("", response_model=list[DashboardSummary])
def list_dashboards(dataset_id: str, db: Db, owner_id: Owner) -> list[DashboardSummary]:
    """Возвращает список дашбордов (заглушка для B5-6)."""
    _require_dataset(db, owner_id, dataset_id)
    raise HTTPException(501, "Dashboard API ещё не реализован")


@router.post("", response_model=DashboardSnapshot)
def create_dashboard(dashboard: DashboardCreate, db: Db, owner_id: Owner) -> DashboardSnapshot:
    """Создает или сохраняет дашборд (заглушка для B5-6)."""
    _require_dataset(db, owner_id, dashboard.dataset_id)
    if dashboard.dashboard_id is not None:
        _require_dashboard(db, owner_id, dashboard.dashboard_id)
    if (
        get_owned_tool_result(db, dashboard.result_id, owner_id, dataset_id=dashboard.dataset_id)
        is None
    ):
        raise HTTPException(404, "Результат не найден")
    raise HTTPException(501, "Dashboard API ещё не реализован")


@router.get("/{dashboard_id}", response_model=DashboardSnapshot)
def get_dashboard(dashboard_id: str, db: Db, owner_id: Owner) -> DashboardSnapshot:
    """Возвращает дашборд по идентификатору (заглушка для B5-6)."""
    _require_dashboard(db, owner_id, dashboard_id)
    raise HTTPException(501, "Dashboard API ещё не реализован")


@router.post("/{dashboard_id}/refresh", response_model=DashboardSnapshot)
def refresh_dashboard(dashboard_id: str, db: Db, owner_id: Owner) -> DashboardSnapshot:
    """Перевыполняет виджеты дашборда (заглушка для B5-6)."""
    _require_dashboard(db, owner_id, dashboard_id)
    raise HTTPException(501, "Dashboard refresh ещё не реализован")


@router.patch("/{dashboard_id}", response_model=DashboardSnapshot)
def update_dashboard(
    dashboard_id: str, patch: DashboardPatch, db: Db, owner_id: Owner
) -> DashboardSnapshot:
    """Согласованный вход PATCH; исполнение относится к B5-6."""
    _require_dashboard(db, owner_id, dashboard_id)
    raise HTTPException(501, "Dashboard API ещё не реализован")
