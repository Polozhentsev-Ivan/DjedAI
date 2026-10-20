"""Роутер дашбордов (B5-6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from djedai.contracts.dashboard import (
    DashboardCreate,
    DashboardPatch,
    DashboardSnapshot,
    DashboardSummary,
)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("", response_model=list[DashboardSummary])
def list_dashboards(dataset_id: str) -> list[DashboardSummary]:
    """Возвращает список дашбордов (заглушка для B5-6)."""
    return []


@router.post("", response_model=DashboardSnapshot)
def create_dashboard(dashboard: DashboardCreate) -> DashboardSnapshot:
    """Создает или сохраняет дашборд (заглушка для B5-6)."""
    raise HTTPException(501, "Dashboard API ещё не реализован")


@router.get("/{dashboard_id}", response_model=DashboardSnapshot)
def get_dashboard(dashboard_id: str) -> DashboardSnapshot:
    """Возвращает дашборд по идентификатору (заглушка для B5-6)."""
    raise HTTPException(501, "Dashboard API ещё не реализован")


@router.post("/{dashboard_id}/refresh", response_model=DashboardSnapshot)
def refresh_dashboard(dashboard_id: str) -> DashboardSnapshot:
    """Перевыполняет виджеты дашборда (заглушка для B5-6)."""
    raise HTTPException(501, "Dashboard refresh ещё не реализован")


@router.patch("/{dashboard_id}", response_model=DashboardSnapshot)
def update_dashboard(dashboard_id: str, patch: DashboardPatch) -> DashboardSnapshot:
    """Согласованный вход PATCH; исполнение относится к B5-6."""
    raise HTTPException(501, "Dashboard API ещё не реализован")
