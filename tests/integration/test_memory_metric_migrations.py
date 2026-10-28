"""Preserve both deployed migration branches when upgrading the combined schema."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, inspect, select

from djedai.backend.db.session import create_db_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "starting_revision",
    ["base", "e5b1c7a2d304", "f166metr0001", "f168qual0001", "f191merge0001"],
)
def test_combined_head_preserves_deployed_branch_data(tmp_path: Path, starting_revision: str):
    url = f"sqlite:///{(tmp_path / 'branches.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    assert ScriptDirectory.from_config(cfg).get_heads() == ["f209model0001"]
    command.upgrade(cfg, starting_revision)
    has_metrics = starting_revision in {"f166metr0001", "f168qual0001", "f191merge0001"}
    has_memory = starting_revision in {"e5b1c7a2d304", "f191merge0001"}
    has_quality = starting_revision == "f168qual0001"
    engine = create_db_engine(url)
    try:
        if starting_revision != "base":
            # Reflection deliberately uses the deployed schema, not the latest ORM.
            metadata = MetaData()
            users = Table("users", metadata, autoload_with=engine)
            datasets = Table("datasets", metadata, autoload_with=engine)
            chats = Table("chats", metadata, autoload_with=engine)
            messages = Table("messages", metadata, autoload_with=engine)
            results = Table("results", metadata, autoload_with=engine)
            now = datetime.now(UTC)
            with engine.begin() as db:
                db.execute(
                    users.insert().values(
                        id="owner", username="owner", is_active=True, created_at=now, updated_at=now
                    )
                )
                db.execute(
                    datasets.insert().values(
                        dataset_id="dataset",
                        owner_id="owner",
                        name="CSV",
                        source_kind="csv",
                        status="ready",
                        created_at=now,
                        updated_at=now,
                    )
                )
                chat = dict(
                    chat_id="chat",
                    owner_id="owner",
                    dataset_id="dataset",
                    title="History",
                    created_at=now,
                    updated_at=now,
                )
                if has_memory:
                    chat["focus_result_ids"] = ["result"]
                db.execute(chats.insert().values(**chat))
                db.execute(
                    messages.insert().values(
                        message_id="question",
                        chat_id="chat",
                        role="user",
                        content="Question",
                        created_at=now,
                    )
                )
                result = dict(result_id="result", owner_id="owner", status="empty", created_at=now)
                if has_metrics:
                    result["metric"] = {"metric_id": "revenue", "version": 1}
                if has_quality:
                    result["quality"] = {"lineage": "complete", "checked": ["orders.amount"]}
                db.execute(results.insert().values(**result))
                if has_metrics:
                    definitions = Table("metric_definitions", metadata, autoload_with=db)
                    proposals = Table("metric_proposals", metadata, autoload_with=db)
                    db.execute(
                        definitions.insert().values(
                            id="definition",
                            owner_id="owner",
                            dataset_id="dataset",
                            metric_id="revenue",
                            version=1,
                            status="active",
                            definition={"name": "Revenue"},
                            created_at=now,
                        )
                    )
                    db.execute(
                        proposals.insert().values(
                            id="proposal",
                            owner_id="owner",
                            dataset_id="dataset",
                            message_id="question",
                            option="Revenue",
                            definition={"name": "Revenue"},
                            created_at=now,
                        )
                    )
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
        schema = inspect(engine)
        assert "focus_result_ids" in {c["name"] for c in schema.get_columns("chats")}
        assert "metric" in {c["name"] for c in schema.get_columns("results")}
        assert "quality" in {c["name"] for c in schema.get_columns("results")}
        assert {"metric_definitions", "metric_proposals"} <= set(schema.get_table_names())
        if starting_revision != "base":
            metadata = MetaData()
            chats = Table("chats", metadata, autoload_with=engine)
            messages = Table("messages", metadata, autoload_with=engine)
            results = Table("results", metadata, autoload_with=engine)
            with engine.connect() as db:
                assert db.scalar(select(messages.c.content)) == "Question"
                assert db.scalar(select(results.c.result_id)) == "result"
                if has_memory:
                    assert db.scalar(select(chats.c.focus_result_ids)) == ["result"]
                if has_metrics:
                    assert db.scalar(select(results.c.metric)) == {
                        "metric_id": "revenue",
                        "version": 1,
                    }
                    assert db.scalar(select(definitions.c.definition)) == {"name": "Revenue"}
                    assert db.scalar(select(proposals.c.message_id)) == "question"
                if has_quality:
                    assert db.scalar(select(results.c.quality)) == {
                        "lineage": "complete",
                        "checked": ["orders.amount"],
                    }
        command.downgrade(cfg, "e161chat0001")
        if starting_revision != "base":
            with engine.connect() as db:
                assert db.scalar(select(messages.c.content)) == "Question"
                assert db.scalar(select(results.c.result_id)) == "result"
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
