"""Question outcome migration preserves existing history in both directions."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from djedai.backend.db.models import MessageModel
from djedai.backend.db.repositories import create_chat, create_dataset, get_or_create_user
from djedai.backend.db.session import create_db_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_chat_turn_upgrade_and_downgrade_preserve_history(tmp_path):
    url = f"sqlite:///{(tmp_path / 'history.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "d8e103060001")
    engine = create_db_engine(url)
    try:
        with Session(engine) as db:
            get_or_create_user(db, "owner", "legacy")
            create_dataset(db, "ds", "owner", "CSV")
            create_chat(db, "chat", "owner", "ds")
            db.execute(
                MessageModel.__table__.insert().values(
                    message_id="question", chat_id="chat", role="user", content="Same text"
                )
            )
            db.commit()
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
        with engine.connect() as db:
            assert db.execute(text("SELECT content, turn FROM messages")).one() == (
                "Same text",
                None,
            )
        command.downgrade(cfg, "d8e103060001")
        assert "turn" not in {column["name"] for column in inspect(engine).get_columns("messages")}
        with engine.connect() as db:
            assert db.scalar(text("SELECT content FROM messages")) == "Same text"
    finally:
        engine.dispose()
