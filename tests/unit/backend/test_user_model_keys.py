"""Operator-assigned credential validation and safe failures, without a model provider."""

from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from djedai.backend.auth.storage import create_user
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.source_secrets import SourceSecretError, SourceSecrets
from djedai.backend.user_model_keys import UserModelKeys


@pytest.fixture
def keys(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'keys.db').as_posix()}")
    init_db(engine)
    service = UserModelKeys(sessionmaker(engine), SourceSecrets(Fernet.generate_key().decode()))
    with service.session_factory() as db:
        user = create_user(db, "alice", "verification-password")
        user_id = user.id
    yield service, user_id
    engine.dispose()


@pytest.mark.parametrize("value", ["", "\n", "hello\rworld", "x" * 8193, 123, False])
def test_invalid_keys_are_rejected_without_echo(keys, value):
    service, user_id = keys
    with pytest.raises(ValueError, match="Недопустимый ключ API"):
        service.assign(user_id, value)


def test_replacement_missing_user_and_key_rotation(keys):
    service, user_id = keys
    assert service.ciphertext(None) is None
    assert service.ciphertext(user_id) is None
    service.assign(user_id, "first-marker")
    service.assign(user_id, "second-marker")
    ciphertext = service.ciphertext(user_id)
    assert service.secrets.decrypt_user_model_key(user_id, ciphertext) == "second-marker"
    service.secrets = SourceSecrets(Fernet.generate_key().decode())
    with pytest.raises(SourceSecretError):
        service.secrets.decrypt_user_model_key(user_id, ciphertext)
    # Clearing remains possible even if the encryption key was lost.
    service.secrets = SourceSecrets(None)
    service.assign(user_id, None)
    service.assign(user_id, None)
    assert service.ciphertext(user_id) is None
    with pytest.raises(HTTPException) as error:
        service.assign("missing-user", None)
    assert error.value.status_code == 404
    with pytest.raises(SourceSecretError):
        service.assign(user_id, "requires-encryption")


def test_storage_failures_are_generic():
    factory = Mock(side_effect=SQLAlchemyError("driver might echo credentials"))
    service = UserModelKeys(factory, SourceSecrets(None))
    for operation in (lambda: service.ciphertext("alice"), lambda: service.assign("alice", None)):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 503
        assert "credentials" not in error.value.detail
