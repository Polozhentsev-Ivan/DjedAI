"""A turn revoked after INSERT must roll back before publishing any assistant."""

import threading
from datetime import UTC, datetime
from time import monotonic

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from djedai.backend.db.models import MessageModel
from djedai.backend.db.repositories import (
    create_chat,
    create_dataset,
    get_or_create_user,
    save_assistant_message,
)
from djedai.backend.db.session import budgeted_write, create_db_engine, init_db
from djedai.contracts.message import AssistantMessage
from djedai.execution import ExecutionBudget, TurnCancelled, TurnTimeout


@pytest.mark.parametrize("cancel", [False, True])
def test_revocation_after_flush_rolls_back_and_restores_connection(tmp_path, cancel) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'write.db'}")
    init_db(engine)
    stopped = threading.Event()
    budget = ExecutionBudget(5, stopped.is_set)

    def revoke_after_insert(_connection, _cursor, statement, *_args):
        if statement.startswith("INSERT INTO messages"):
            if cancel:
                stopped.set()
            else:
                budget.deadline = monotonic() - 1

    try:
        with Session(engine) as db:
            get_or_create_user(db, "owner", "owner")
            create_dataset(db, "dataset", "owner", "fixture")
            create_chat(db, "chat", "owner", "dataset")
            message = AssistantMessage(
                message_id="assistant",
                chat_id="chat",
                role="assistant",
                created_at=datetime.now(UTC),
                blocks=[{"type": "text", "markdown": "verified"}],
            )
            event.listen(engine, "after_cursor_execute", revoke_after_insert)
            try:
                with pytest.raises(TurnCancelled if cancel else TurnTimeout):
                    save_assistant_message(db, message, budget=budget)
            finally:
                event.remove(engine, "after_cursor_execute", revoke_after_insert)
            assert list(db.scalars(select(MessageModel))) == []
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            stopped.clear()
            save_assistant_message(db, message, budget=ExecutionBudget(5))
            assert len(list(db.scalars(select(MessageModel)))) == 1
    finally:
        engine.dispose()


def test_non_lock_database_error_is_not_retried(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'error.db'}")
    calls = []

    def invalid_write(db: Session):
        calls.append(True)
        db.execute(text("SELECT * FROM missing_table"))

    try:
        with Session(engine) as db:
            with pytest.raises(OperationalError):
                budgeted_write(db, ExecutionBudget(5), invalid_write)
            assert len(calls) == 1
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
    finally:
        engine.dispose()
