"""Подтверждённые определения бизнес-метрик датасета и их версии (B2-9).

Определение подтверждает пользователь: запросом к API или выбором варианта уточнения,
к которому агент приложил кандидата. Каждое подтверждение создаёт новую неизменяемую
версию, предыдущая получает ``retired``. Перед сохранением определение сверяется с текущим
каталогом датасета; кандидаты от агента подтверждением не считаются.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import (
    MessageModel,
    MetricDefinitionModel,
    MetricProposalModel,
)
from djedai.contracts.metrics import ConfirmationSource, MetricConfirmation, MetricDefinition

logger = logging.getLogger(__name__)


class MetricService:
    def __init__(self, session_factory: sessionmaker[Session], datasets: DatasetService) -> None:
        self.session_factory = session_factory
        self.datasets = datasets

    def list(
        self, owner_id: str, dataset_id: str, *, history: bool = False
    ) -> list[MetricDefinition]:
        """Действующие определения; с ``history`` — все версии, включая выведенные."""
        self.datasets.get_detail(owner_id, dataset_id)
        with self.session_factory() as db:
            query = select(MetricDefinitionModel).where(
                MetricDefinitionModel.owner_id == owner_id,
                MetricDefinitionModel.dataset_id == dataset_id,
            )
            if not history:
                query = query.where(MetricDefinitionModel.status == "confirmed")
            rows = db.scalars(
                query.order_by(MetricDefinitionModel.metric_id, MetricDefinitionModel.version)
            )
            return [_definition(row) for row in rows]

    def confirmed(self, owner_id: str, dataset_id: str) -> dict[str, MetricDefinition]:
        return {m.metric_id: m for m in self.list(owner_id, dataset_id)}

    def confirm(
        self,
        owner_id: str,
        dataset_id: str,
        definition: MetricDefinition,
        *,
        source: ConfirmationSource = "api",
        message_id: str | None = None,
    ) -> MetricDefinition:
        """Сохраняет новую подтверждённую версию; ``version``/``status`` клиента игнорируются."""
        catalog = self.datasets.get_catalog(owner_id, dataset_id)
        try:
            definition.validate_against(catalog, self.confirmed(owner_id, dataset_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        with self.session_factory() as db:
            current = db.scalars(
                select(MetricDefinitionModel).where(
                    MetricDefinitionModel.dataset_id == dataset_id,
                    MetricDefinitionModel.metric_id == definition.metric_id,
                    MetricDefinitionModel.status == "confirmed",
                )
            ).all()
            version = (
                db.scalar(
                    select(func.max(MetricDefinitionModel.version)).where(
                        MetricDefinitionModel.dataset_id == dataset_id,
                        MetricDefinitionModel.metric_id == definition.metric_id,
                    )
                )
                or 0
            ) + 1
            confirmed = MetricDefinition.model_validate(
                {
                    **definition.model_dump(mode="json"),
                    "version": version,
                    "status": "confirmed",
                    "confirmation": MetricConfirmation(
                        source=source,
                        by_user_id=owner_id,
                        at=datetime.now(UTC),
                        message_id=message_id,
                    ).model_dump(mode="json"),
                }
            )
            for row in current:
                _set_status(row, "retired")
            db.add(
                MetricDefinitionModel(
                    id=f"md_{uuid4().hex}",
                    owner_id=owner_id,
                    dataset_id=dataset_id,
                    metric_id=confirmed.metric_id,
                    version=version,
                    status="confirmed",
                    definition=confirmed.model_dump(mode="json"),
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(
                    status_code=409, detail="Определение метрики изменилось, повторите запрос"
                ) from None
            return confirmed

    def retire(self, owner_id: str, dataset_id: str, metric_id: str) -> None:
        """Выводит метрику из употребления; после этого она снова не определена."""
        confirmed = self.confirmed(owner_id, dataset_id)
        if metric_id not in confirmed:
            raise HTTPException(status_code=404, detail="Метрика не определена")
        users = sorted(
            m.metric_id for m in confirmed.values() if metric_id in (m.numerator, m.denominator)
        )
        if users:
            raise HTTPException(
                status_code=409, detail=f"Метрика используется в {', '.join(users)}"
            )
        with self.session_factory() as db:
            rows = db.scalars(
                select(MetricDefinitionModel).where(
                    MetricDefinitionModel.owner_id == owner_id,
                    MetricDefinitionModel.dataset_id == dataset_id,
                    MetricDefinitionModel.metric_id == metric_id,
                    MetricDefinitionModel.status == "confirmed",
                )
            )
            for row in rows:
                _set_status(row, "retired")
            db.commit()

    def propose(
        self,
        db: Session,
        owner_id: str,
        dataset_id: str,
        message_id: str,
        candidates: Sequence[tuple[str, MetricDefinition]],
    ) -> None:
        """Запоминает кандидатов уточнения в транзакции сообщения ``db``; без commit."""
        for option, definition in candidates:
            proposal = definition.model_copy(update={"status": "proposed", "confirmation": None})
            db.add(
                MetricProposalModel(
                    id=f"mp_{uuid4().hex}",
                    owner_id=owner_id,
                    dataset_id=dataset_id,
                    message_id=message_id,
                    option=option,
                    definition=proposal.model_dump(mode="json"),
                )
            )

    def confirm_choice(
        self, owner_id: str, dataset_id: str, chat_id: str, text: str, before_message_id: str
    ) -> MetricDefinition | None:
        """Подтверждает кандидата, если ``text`` — вариант последнего ответа ассистента."""
        with self.session_factory() as db:
            question = db.get(MessageModel, before_message_id)
            query = select(MessageModel).where(
                MessageModel.chat_id == chat_id, MessageModel.role == "assistant"
            )
            if question is not None:
                query = query.where(MessageModel.created_at <= question.created_at)
            last = db.scalars(
                query.order_by(MessageModel.created_at.desc(), MessageModel.message_id.desc())
            ).first()
            if last is None:
                return None
            proposal = db.scalars(
                select(MetricProposalModel).where(
                    MetricProposalModel.owner_id == owner_id,
                    MetricProposalModel.dataset_id == dataset_id,
                    MetricProposalModel.message_id == last.message_id,
                    MetricProposalModel.option == text.strip(),
                )
            ).first()
            if proposal is None:
                return None
            definition = MetricDefinition.model_validate(proposal.definition)
            message_id = last.message_id
        try:
            return self.confirm(
                owner_id, dataset_id, definition, source="clarification", message_id=message_id
            )
        except HTTPException as error:
            # Каталог мог измениться после уточнения: агент спросит снова.
            logger.info("Metric choice was not confirmed: %s", error.detail)
            return None


def _definition(row: MetricDefinitionModel) -> MetricDefinition:
    return MetricDefinition.model_validate(row.definition)


def _set_status(row: MetricDefinitionModel, status: str) -> None:
    row.status = status
    row.definition = {**row.definition, "status": status}
