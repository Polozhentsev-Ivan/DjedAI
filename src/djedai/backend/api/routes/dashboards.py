"""Роутер дашбордов (B5-6)."""

from __future__ import annotations

from fastapi import APIRouter

from djedai.contracts.dashboard import Dashboard

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("", response_model=list[Dashboard])
def list_dashboards(dataset_id: str | None = None) -> list[Dashboard]:
    """Возвращает список дашбордов (заглушка для B5-6)."""
    return []


@router.post("", response_model=Dashboard, status_code=201)
def create_dashboard(dashboard: Dashboard) -> Dashboard:
    """Создает или сохраняет дашборд (заглушка для B5-6)."""
    return dashboard


@router.get("/{dashboard_id}", response_model=Dashboard)
def get_dashboard(dashboard_id: str) -> Dashboard:
    """Возвращает дашборд по идентификатору (заглушка для B5-6)."""
    return Dashboard(dashboard_id=dashboard_id, dataset_id="ds_stub", title="Дашборд")


@router.post("/{dashboard_id}/refresh", response_model=Dashboard)
def refresh_dashboard(dashboard_id: str) -> Dashboard:
    """Перевыполняет виджеты дашборда (заглушка для B5-6)."""
    return Dashboard(dashboard_id=dashboard_id, dataset_id="ds_stub", title="Обновленный дашборд")
