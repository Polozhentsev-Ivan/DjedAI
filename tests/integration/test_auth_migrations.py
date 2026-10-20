"""AUTH-1: реальные миграции, ограничения и перезапуск на временной SQLite."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from djedai.backend.auth.storage import create_session, create_user, get_session_user
from djedai.backend.db.models import (
    ChatModel,
    DashboardModel,
    DatasetModel,
    SessionModel,
    ToolResultModel,
    UserModel,
)
from djedai.backend.db.repositories import (
    create_chat,
    create_dashboard,
    create_dataset,
    get_or_create_user,
)
from djedai.backend.db.session import create_db_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PRE_AUTH = "a5c2f58e6aea"


def migration_config(db_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migrations_create_schema_and_enforce_constraints(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'auth.db').as_posix()}"
    cfg = migration_config(url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # Повторный запуск безопасен.
    engine = create_db_engine(url)
    try:
        schema = inspect(engine)
        assert {"users", "sessions", "datasets", "chats", "results", "dashboards"} <= set(
            schema.get_table_names()
        )
        assert {c["name"] for c in schema.get_columns("users")} >= {
            "username",
            "password_hash",
            "is_active",
            "created_at",
            "updated_at",
        }
        for table, column in [
            ("sessions", "user_id"),
            ("datasets", "owner_id"),
            ("chats", "owner_id"),
            ("results", "owner_id"),
            ("dashboards", "owner_id"),
        ]:
            assert any(
                fk["constrained_columns"] == [column]
                and fk["referred_table"] == "users"
                and fk["options"]["ondelete"] == "CASCADE"
                for fk in schema.get_foreign_keys(table)
            )
        with Session(engine) as db:
            user = create_user(db, "alice", "password-alice")
            assert user.password_hash.startswith("$argon2id$")
            assert user.is_active and user.created_at.tzinfo == UTC
            assert user.updated_at >= user.created_at
            with pytest.raises(IntegrityError):
                create_user(db, "alice", "another-password")
            db.rollback()
            now = datetime.now(UTC)
            invalid_sessions = [
                SessionModel(
                    token_hash="short",
                    user_id=user.id,
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                ),
                SessionModel(
                    token_hash="0" * 64,
                    user_id="missing-owner",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                ),
                SessionModel(token_hash="1" * 64, user_id=user.id, created_at=now, expires_at=now),
            ]
            for invalid in invalid_sessions:
                db.add(invalid)
                with pytest.raises(IntegrityError):
                    db.commit()
                db.rollback()
            create_session(db, user.id)
            existing = db.scalar(select(SessionModel))
            with pytest.raises(IntegrityError):
                db.execute(
                    SessionModel.__table__.insert().values(
                        token_hash=existing.token_hash,
                        user_id=user.id,
                        created_at=now,
                        expires_at=now + timedelta(days=1),
                    )
                )
            db.rollback()
    finally:
        engine.dispose()


def test_upgrade_and_auth_downgrade_preserve_existing_owners(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'existing.db').as_posix()}"
    cfg = migration_config(url)
    command.upgrade(cfg, PRE_AUTH)
    engine = create_db_engine(url)
    try:
        with Session(engine) as db:
            get_or_create_user(db, "legacy", "legacy-owner")  # Не умеет входить без пароля.
            create_dataset(db, "dataset", "legacy", "CSV")
            create_chat(db, "chat", "legacy", "dataset")
            create_dashboard(db, "dashboard", "legacy", "dataset", "Обзор")
            db.add(ToolResultModel(result_id="result", owner_id="legacy", status="empty"))
            db.commit()
        command.upgrade(cfg, "head")
        with Session(engine) as db:
            user = db.get(UserModel, "legacy")
            assert user.password_hash is None
            assert db.get(DatasetModel, "dataset").owner_id == "legacy"
            assert db.get(ChatModel, "chat").owner_id == "legacy"
            assert db.get(DashboardModel, "dashboard").owner_id == "legacy"
            assert db.get(ToolResultModel, "result").owner_id == "legacy"
        command.downgrade(cfg, PRE_AUTH)
        assert "sessions" not in inspect(engine).get_table_names()
        with Session(engine) as db:
            assert db.get(UserModel, "legacy").username == "legacy-owner"
            assert db.get(DatasetModel, "dataset").owner_id == "legacy"
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()


def test_session_and_ownership_survive_new_process(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'restart.db').as_posix()}"
    command.upgrade(migration_config(url), "head")
    engine = create_db_engine(url)
    try:
        with Session(engine) as db:
            user = create_user(db, "restart-user", "restart-password")
            owner = user.id
            token = create_session(db, owner)
            token_hash = sha256(token.encode()).hexdigest()
            create_dataset(db, "ds", owner, "Данные")
            create_chat(db, "chat", owner, "ds")
            create_dashboard(db, "dashboard", owner, "ds", "Дашборд")
            db.add(ToolResultModel(result_id="result", owner_id=owner, status="empty"))
            db.commit()
    finally:
        engine.dispose()
    script = """
import json, sys
from sqlalchemy.orm import Session
from djedai.backend.auth.storage import authenticate_user, get_session_user
from djedai.backend.db.models import DatasetModel, ChatModel, DashboardModel, ToolResultModel
from djedai.backend.db.session import create_db_engine
payload = json.load(sys.stdin)
engine = create_db_engine(payload['url'])
with Session(engine) as db:
    user = get_session_user(db, payload['token'])
    logged_in = authenticate_user(db, 'restart-user', 'restart-password')
    print(json.dumps([user.id, logged_in.id,
        db.get(DatasetModel, 'ds').owner_id, db.get(ChatModel, 'chat').owner_id,
        db.get(DashboardModel, 'dashboard').owner_id, db.get(ToolResultModel, 'result').owner_id]))
engine.dispose()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"url": url, "token": token}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=os.environ | {"PYTHONPATH": str(ROOT / "src")},
    )
    assert json.loads(completed.stdout) == [owner] * 6
    # Удаление через SQL, без ORM cascade, проверяет реальный FK в миграции.
    engine = create_db_engine(url)
    try:
        with Session(engine) as db:
            other = create_user(db, "another-user", "another-password")
            other_id = other.id
            other_token = create_session(db, other_id)
            db.execute(delete(UserModel).where(UserModel.id == owner))
            db.commit()
            assert db.get(SessionModel, token_hash) is None
            assert get_session_user(db, token) is None
            assert get_session_user(db, other_token).id == other_id
            assert db.get(DatasetModel, "ds") is None
            assert db.get(ChatModel, "chat") is None
            assert db.get(DashboardModel, "dashboard") is None
            assert db.get(ToolResultModel, "result") is None
    finally:
        engine.dispose()
