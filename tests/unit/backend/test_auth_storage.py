"""Проверки паролей, регистрации и серверных сессий AUTH-1."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from argon2 import extract_parameters
from argon2.low_level import Type
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from djedai.backend.auth import (
    authenticate_user,
    create_session,
    create_user,
    get_session_user,
    hash_password,
    revoke_session,
    storage,
    verify_password,
)
from djedai.backend.db.models import SessionModel, UserModel
from djedai.backend.db.session import create_db_engine, init_db

_PASSWORD = " мой пароль 123 "


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(_PASSWORD)


@pytest.fixture
def user(db: Session, password_hash: str) -> UserModel:
    record = UserModel(id="existing-user", username="alice", password_hash=password_hash)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def test_password_hash_uses_argon2id_and_random_salts() -> None:
    first = hash_password(_PASSWORD)
    second = hash_password(_PASSWORD)

    assert first != second
    assert _PASSWORD not in first
    assert verify_password(_PASSWORD, first)
    assert verify_password(_PASSWORD, second)
    params = extract_parameters(first)
    assert params.type is Type.ID
    assert params.memory_cost >= 65_536
    assert params.time_cost >= 3
    assert params.salt_len >= 16


def test_password_is_not_stripped_or_normalized(password_hash: str) -> None:
    assert not verify_password(_PASSWORD.strip(), password_hash)
    assert not verify_password(" мой пароль 124 ", password_hash)
    unicode_hash = hash_password("пароль-é")
    assert verify_password("пароль-é", unicode_hash)
    assert not verify_password("пароль-e\u0301", unicode_hash)


@pytest.mark.parametrize("bad_hash", [None, "", "not-a-hash", "$argon2id$broken", "пароль"])
def test_invalid_or_missing_hash_cannot_authenticate(bad_hash: str | None) -> None:
    assert not verify_password(_PASSWORD, bad_hash)


@pytest.mark.parametrize("password", ["", "short", "x" * 1_025])
def test_hash_password_rejects_invalid_length(password: str) -> None:
    with pytest.raises(ValueError, match="Пароль"):
        hash_password(password)


def test_password_length_boundaries_and_long_input(password_hash: str) -> None:
    for password in ("123456", "я" * 1_024):
        assert verify_password(password, hash_password(password))
    assert not verify_password("x" * 1_025, password_hash)


def test_create_user_persists_hash_and_account_metadata(db: Session) -> None:
    user = create_user(db, "новый_логин", _PASSWORD, "alice@example.org")
    user_id = user.id
    assert UUID(user_id).version == 4
    assert len(user_id) == 32
    assert user.username == "новый_логин"
    assert user.email == "alice@example.org"
    assert user.is_active
    assert user.created_at.tzinfo is UTC
    assert user.updated_at.tzinfo is UTC
    assert user.password_hash != _PASSWORD
    assert verify_password(_PASSWORD, user.password_hash)

    with Session(db.get_bind()) as other_session:
        persisted = other_session.get(UserModel, user_id)
        assert persisted is not None
        assert persisted.password_hash == user.password_hash
        assert authenticate_user(other_session, "новый_логин", _PASSWORD).id == user_id


def test_username_unique_index_is_case_sensitive(db: Session) -> None:
    first = create_user(db, "Alice", _PASSWORD)
    second = create_user(db, "alice", _PASSWORD)
    assert first.id != second.id
    assert first.email is None
    with pytest.raises(IntegrityError):
        create_user(db, "Alice", "another password")
    db.rollback()
    assert authenticate_user(db, "Alice", _PASSWORD).id == first.id
    assert authenticate_user(db, "alice", _PASSWORD).id == second.id


@pytest.mark.parametrize("username", ["", "aa", " " * 3, "\t\n ", "a" * 51])
def test_create_user_rejects_invalid_username(db: Session, username: str) -> None:
    with pytest.raises(ValueError, match="Логин"):
        create_user(db, username, _PASSWORD)
    assert db.scalars(select(UserModel)).all() == []


def test_create_user_rejects_oversized_email(db: Session) -> None:
    with pytest.raises(ValueError, match="Email"):
        create_user(db, "alice", _PASSWORD, "x" * 256)


def test_authentication_checks_password_username_and_active_state(
    db: Session, user: UserModel
) -> None:
    assert authenticate_user(db, "alice", _PASSWORD).id == user.id
    assert authenticate_user(db, "Alice", _PASSWORD) is None
    assert authenticate_user(db, "nobody", _PASSWORD) is None
    assert authenticate_user(db, "alice", "wrong password") is None
    user.is_active = False
    db.commit()
    assert authenticate_user(db, "alice", _PASSWORD) is None


@pytest.mark.parametrize("bad_hash", [None, "", "broken-hash"])
def test_legacy_user_without_valid_password_cannot_authenticate(
    db: Session, user: UserModel, bad_hash: str | None
) -> None:
    user.password_hash = bad_hash
    db.commit()
    assert authenticate_user(db, "alice", _PASSWORD) is None


@pytest.mark.parametrize("replacement", ["valid", None, "broken-hash"])
def test_authentication_uses_persisted_password_without_overwriting_pending_changes(
    db: Session, user: UserModel, password_hash: str, replacement: str | None
) -> None:
    db.autoflush = False  # The application session factory uses this setting.
    user.email = "pending@example.org"
    replacement_hash = hash_password("new-password") if replacement == "valid" else replacement
    with Session(db.get_bind()) as writer:
        record = writer.get(UserModel, user.id)
        record.password_hash = replacement_hash
        writer.commit()

    # Retain the old object so SQLAlchemy's identity map still contains the old hash.
    assert user.password_hash == password_hash
    assert authenticate_user(db, "alice", _PASSWORD) is None
    if replacement == "valid":
        assert authenticate_user(db, "alice", "new-password").id == user.id
    else:
        assert authenticate_user(db, "alice", "new-password") is None
    assert user.email == "pending@example.org"
    assert db.is_modified(user)
    db.commit()
    with Session(db.get_bind()) as reader:
        persisted = reader.get(UserModel, user.id)
        assert persisted.email == "pending@example.org"
        assert persisted.password_hash == replacement_hash


def test_session_persists_only_token_digest_and_survives_new_db_session(
    db: Session, user: UserModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(storage, "_utc_now", lambda: now)
    token = create_session(db, user.id)
    assert len(token) == 43
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = db.get(SessionModel, digest)
    assert record is not None
    assert record.token_hash != token
    assert record.user_id == user.id
    assert record.created_at == now
    assert record.expires_at == now + timedelta(days=1)
    assert record.expires_at.tzinfo is UTC
    assert record.revoked_at is None
    stored_values = db.execute(SessionModel.__table__.select()).one()
    assert token not in stored_values
    with Session(db.get_bind()) as other_session:
        assert get_session_user(other_session, token).id == user.id


def test_expired_session_is_rejected_at_expiration_boundary(
    db: Session, user: UserModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(storage, "_utc_now", lambda: now)
    token = create_session(db, user.id, ttl=timedelta(minutes=5))
    monkeypatch.setattr(storage, "_utc_now", lambda: now + timedelta(minutes=5))
    assert get_session_user(db, token) is None


@pytest.mark.parametrize("token", ["", "unknown-token", "\ud800", "token-\udfff", "токен"])
def test_unknown_session_cannot_authenticate_or_be_revoked(db: Session, token: str) -> None:
    assert get_session_user(db, token) is None
    assert not revoke_session(db, token)


def test_revoke_session_is_persistent_and_idempotent(db: Session, user: UserModel) -> None:
    token = create_session(db, user.id)
    assert revoke_session(db, token)
    assert get_session_user(db, token) is None
    assert not revoke_session(db, token)
    with Session(db.get_bind()) as other_session:
        record = other_session.get(SessionModel, hashlib.sha256(token.encode()).hexdigest())
        assert record.revoked_at is not None
        assert record.revoked_at.tzinfo is UTC
        assert get_session_user(other_session, token) is None


def test_sessions_have_distinct_tokens_and_can_be_revoked_independently(
    db: Session, user: UserModel
) -> None:
    first = create_session(db, user.id)
    second = create_session(db, user.id)
    assert first != second
    assert revoke_session(db, first)
    assert get_session_user(db, second).id == user.id


def test_deactivated_user_cannot_use_existing_session(db: Session, user: UserModel) -> None:
    token = create_session(db, user.id)
    user.is_active = False
    db.commit()
    assert get_session_user(db, token) is None


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), "1 day", None])
def test_invalid_session_ttl_is_rejected(db: Session, user: UserModel, ttl: timedelta) -> None:
    with pytest.raises(ValueError, match="Срок жизни"):
        create_session(db, user.id, ttl=ttl)
    assert db.scalars(select(SessionModel)).all() == []


def test_session_ttl_cannot_overflow_datetime(db: Session, user: UserModel) -> None:
    with pytest.raises(ValueError, match="диапазон"):
        create_session(db, user.id, ttl=timedelta.max)


def test_session_requires_existing_active_user_with_password(db: Session, user: UserModel) -> None:
    with pytest.raises(ValueError, match="пользователь"):
        create_session(db, "missing-user")
    user.is_active = False
    db.commit()
    with pytest.raises(ValueError, match="пользователь"):
        create_session(db, user.id)
    user.is_active = True
    for no_password in (None, ""):
        user.password_hash = no_password
        db.commit()
        with pytest.raises(ValueError, match="пользователь"):
            create_session(db, user.id)
    assert db.scalars(select(SessionModel)).all() == []
