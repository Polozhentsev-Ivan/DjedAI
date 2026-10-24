"""Подтверждённые определения бизнес-метрик датасета (B2-9)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from djedai.backend.auth.dependencies import current_owner_id
from djedai.backend.metric_service import MetricService
from djedai.contracts.metrics import MetricDefinition

router = APIRouter(prefix="/datasets/{dataset_id}/metrics", tags=["metrics"])


def get_metric_service(request: Request) -> MetricService:
    return request.app.state.metric_service


MetricServiceDep = Annotated[MetricService, Depends(get_metric_service)]
OwnerDep = Annotated[str, Depends(current_owner_id)]


@router.get("", response_model=list[MetricDefinition])
def list_metrics(
    dataset_id: str, owner_id: OwnerDep, service: MetricServiceDep, history: bool = False
) -> list[MetricDefinition]:
    """Действующие определения; ``history=true`` — все версии, включая выведенные."""
    return service.list(owner_id, dataset_id, history=history)


@router.post("", response_model=MetricDefinition, status_code=201)
def confirm_metric(
    dataset_id: str, payload: MetricDefinition, owner_id: OwnerDep, service: MetricServiceDep
) -> MetricDefinition:
    """Подтверждает определение новой версией; ``version``, ``status`` и ``confirmation``
    назначает сервер."""
    return service.confirm(owner_id, dataset_id, payload)


@router.delete("/{metric_id}", status_code=204)
def retire_metric(
    dataset_id: str, metric_id: str, owner_id: OwnerDep, service: MetricServiceDep
) -> Response:
    service.retire(owner_id, dataset_id, metric_id)
    return Response(status_code=204)
