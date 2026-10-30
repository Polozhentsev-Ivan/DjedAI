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
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from djedai.backend.auth.storage import create_session, create_user, get_session_user
from djedai.backend.db.base import Base
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


def test_migration_chain_has_a_single_head(tmp_path: Path) -> None:
    """Параллельные ветки миграций не должны давать Multiple head revisions."""
    cfg = migration_config(f"sqlite:///{(tmp_path / 'heads.db').as_posix()}")
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_user_model_keys_migrate_and_survive_restart(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet
    from sqlalchemy.orm import sessionmaker

    from djedai.backend.db.models import UserModelKeyModel
    from djedai.backend.source_secrets import SourceSecrets
    from djedai.backend.user_model_keys import UserModelKeys

    url = f"sqlite:///{(tmp_path / 'user-keys.db').as_posix()}"
    cfg = migration_config(url)
    command.upgrade(cfg, "f211merge0001")
    engine = create_db_engine(url)
    with Session(engine) as db:
        user_id = create_user(db, "migration-user", "migration-password").id
    command.upgrade(cfg, "head")
    secrets = SourceSecrets(Fernet.generate_key().decode())
    keys = UserModelKeys(sessionmaker(engine), secrets)
    keys.assign(user_id, "migration-key-marker")
    engine.dispose()
    engine = create_db_engine(url)
    try:
        keys = UserModelKeys(sessionmaker(engine), secrets)
        assert (
            secrets.decrypt_user_model_key(user_id, keys.ciphertext(user_id))
            == "migration-key-marker"
        )
        with Session(engine) as db:
            db.execute(delete(UserModel).where(UserModel.id == user_id))
            db.commit()
            assert db.get(UserModelKeyModel, user_id) is None
        command.downgrade(cfg, "f211merge0001")
        assert "user_model_keys" not in inspect(engine).get_table_names()
        assert "users" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


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
        assert {"summary", "focus_result_ids"} <= {c["name"] for c in schema.get_columns("chats")}
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
            # Seed the historical revision using only columns it actually had.
            # The current ORM also knows storage columns introduced after AUTH-1.
            db.execute(
                DatasetModel.__table__.insert().values(
                    dataset_id="dataset",
                    owner_id="legacy",
                    name="CSV",
                    source_kind="csv",
                    status="ready",
                )
            )
            db.commit()
            db.execute(
                ChatModel.__table__.insert().values(
                    chat_id="chat",
                    owner_id="legacy",
                    dataset_id="dataset",
                    title="Новый чат",
                    summary=None,
                )
            )
            db.commit()
            # This fixture predates dashboard consistency/provenance columns.
            dashboards = Table("dashboards", MetaData(), autoload_with=db.connection())
            db.execute(
                dashboards.insert().values(
                    dashboard_id="dashboard",
                    owner_id="legacy",
                    dataset_id="dataset",
                    title="Обзор",
                    widgets=[],
                    created_at=datetime.now(UTC),
                )
            )
            db.commit()
            db.execute(
                ToolResultModel.__table__.insert().values(
                    result_id="result", owner_id="legacy", status="empty"
                )
            )
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
            assert (
                db.scalar(select(DatasetModel.owner_id).where(DatasetModel.dataset_id == "dataset"))
                == "legacy"
            )
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


def test_model_settings_migration_matches_model_and_downgrades(tmp_path: Path) -> None:
    from djedai.backend.db.models import ModelSettingsModel

    url = f"sqlite:///{(tmp_path / 'model-settings.db').as_posix()}"
    cfg = migration_config(url)
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("model_settings")}
        assert columns == set(ModelSettingsModel.__table__.columns.keys())
        with Session(engine) as db:
            db.add(ModelSettingsModel(id="default", model_name="m", api_key_ciphertext="c"))
            db.commit()
            assert db.get(ModelSettingsModel, "default").updated_at.tzinfo is not None
    finally:
        engine.dispose()
    command.downgrade(cfg, "f169read0001")
    engine = create_db_engine(url)
    try:
        assert "model_settings" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize("deployed_revision", ["f209model0001", "f178sens0001"])
def test_merge_upgrade_preserves_both_deployed_branches(
    tmp_path: Path, deployed_revision: str
) -> None:
    """Existing installations on either branch must receive the other branch's schema."""
    from djedai.backend.db.models import ModelSettingsModel

    url = f"sqlite:///{(tmp_path / f'{deployed_revision}.db').as_posix()}"
    cfg = migration_config(url)
    command.upgrade(cfg, deployed_revision)
    engine = create_db_engine(url)
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    try:
        # Reflect the deployed schema: current ORM defaults/columns do not belong
        # in fixtures for revisions which predate them.
        historical = MetaData()
        historical.reflect(bind=engine)
        with engine.begin() as connection:
            connection.execute(
                historical.tables["users"]
                .insert()
                .values(
                    id="existing-owner",
                    username="existing-owner",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                historical.tables["datasets"]
                .insert()
                .values(
                    dataset_id="existing-dataset",
                    owner_id="existing-owner",
                    name="Existing",
                    source_kind="csv",
                    status="ready",
                    created_at=now,
                    updated_at=now,
                )
            )
            dashboard_values = dict(
                dashboard_id="existing-dashboard",
                owner_id="existing-owner",
                dataset_id="existing-dataset",
                title="Existing dashboard",
                widgets=[],
                created_at=now,
            )
            result_values = dict(
                result_id="existing-result",
                owner_id="existing-owner",
                status="ok",
                created_at=now,
            )
            if deployed_revision == "f178sens0001":
                dashboard_values["consistency"] = "snapshot"
                result_values["sensitive_columns"] = ["email", "phone"]
            else:
                connection.execute(
                    historical.tables["model_settings"]
                    .insert()
                    .values(
                        id="default",
                        base_url="https://models.example/v1",
                        model_name="existing",
                        api_key_ciphertext="existing-ciphertext",
                        updated_by="existing-owner",
                        updated_at=now,
                    )
                )
            connection.execute(historical.tables["dashboards"].insert().values(**dashboard_values))
            connection.execute(historical.tables["results"].insert().values(**result_values))

        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
        schema = inspect(engine)
        for table in Base.metadata.sorted_tables:
            assert set(table.columns.keys()) == {
                column["name"] for column in schema.get_columns(table.name)
            }, table.name
        with engine.connect() as connection:
            versions = Table("alembic_version", MetaData(), autoload_with=connection)
            assert (
                connection.scalars(select(versions.c.version_num)).all()
                == ScriptDirectory.from_config(cfg).get_heads()
            )
        with Session(engine) as db:
            dashboard = db.get(DashboardModel, "existing-dashboard")
            result = db.get(ToolResultModel, "existing-result")
            assert dashboard.owner_id == result.owner_id == "existing-owner"
            assert dashboard.dataset_id == "existing-dataset"
            assert dashboard.title == "Existing dashboard"
            assert result.status == "ok"
            if deployed_revision == "f178sens0001":
                assert dashboard.consistency == "snapshot"
                assert result.sensitive_columns == ["email", "phone"]
            else:
                assert dashboard.consistency == "independent"
                assert result.sensitive_columns is None
                settings = db.get(ModelSettingsModel, "default")
                assert settings.base_url == "https://models.example/v1"
                assert settings.model_name == "existing"
                assert settings.api_key_ciphertext == "existing-ciphertext"
                assert settings.updated_by == "existing-owner"
                assert settings.updated_at == now
    finally:
        engine.dispose()
    command.downgrade(cfg, "f169read0001")
    engine = create_db_engine(url)
    try:
        schema = inspect(engine)
        assert "model_settings" not in schema.get_table_names()
        assert "consistency" not in {c["name"] for c in schema.get_columns("dashboards")}
        assert "sensitive_columns" not in {c["name"] for c in schema.get_columns("results")}
        with engine.connect() as connection:
            dashboards = Table("dashboards", MetaData(), autoload_with=connection)
            results = Table("results", MetaData(), autoload_with=connection)
            assert connection.scalar(select(dashboards.c.title)) == "Existing dashboard"
            assert connection.scalar(select(results.c.status)) == "ok"
    finally:
        engine.dispose()
