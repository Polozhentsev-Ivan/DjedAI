"""Source credentials are authenticated, owner-bound and fail closed."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from djedai.backend.source_secrets import SourceSecretError, SourceSecrets

DSN = "postgresql://reader:private-password@source.internal:5432/business"


def test_round_trip_hides_plaintext_and_randomizes_identical_sources() -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    ciphertext = store.encrypt("alice", "dataset-a", DSN, ["public.sales"])
    other_ciphertext = store.encrypt("alice", "dataset-a", DSN, ["public.sales"])
    assert ciphertext != other_ciphertext
    for secret in (DSN, "private-password", "source.internal", "public.sales"):
        assert secret not in ciphertext
    source = store.decrypt("alice", "dataset-a", ciphertext)
    assert source.dsn == DSN
    assert source.tables == ("public.sales",)
    assert DSN not in repr(source)
    assert "private-password" not in repr(source)
    all_tables = store.encrypt("alice", "dataset-b", DSN, None)
    assert store.decrypt("alice", "dataset-b", all_tables).tables is None


@pytest.mark.parametrize("owner,dataset", [("bob", "dataset-a"), ("alice", "dataset-b")])
def test_ciphertext_cannot_be_transplanted_to_another_resource(owner, dataset) -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    ciphertext = store.encrypt("alice", "dataset-a", DSN, None)
    with pytest.raises(SourceSecretError):
        store.decrypt(owner, dataset, ciphertext)


def test_secret_is_bound_to_its_source_kind() -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    clickhouse = "http://reader:private-password@ch.internal:8123/business"
    ciphertext = store.encrypt("alice", "dataset-a", clickhouse, None, kind="clickhouse")
    assert store.decrypt("alice", "dataset-a", ciphertext, kind="clickhouse").dsn == clickhouse
    with pytest.raises(SourceSecretError):
        store.decrypt("alice", "dataset-a", ciphertext)
    with pytest.raises(SourceSecretError):
        store.decrypt(
            "alice", "dataset-a", store.encrypt("alice", "dataset-a", DSN, None), kind="clickhouse"
        )
    with pytest.raises(SourceSecretError):
        store.encrypt("alice", "dataset-a", DSN, None, kind="mysql")


@pytest.mark.parametrize("key", [None, "", "invalid", "я" * 32])
def test_missing_or_invalid_key_never_stores_plaintext(key) -> None:
    store = SourceSecrets(key)
    with pytest.raises(SourceSecretError):
        store.encrypt("alice", "dataset-a", DSN, None)
    with pytest.raises(SourceSecretError):
        store.decrypt("alice", "dataset-a", "invalid")


def test_bad_ciphertext_and_wrong_key_fail_without_secret_error_details() -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    ciphertext = store.encrypt("alice", "dataset-a", DSN, None)
    another_store = SourceSecrets(Fernet.generate_key().decode())
    for reader, value in (
        (store, ciphertext[:-8] + "AAAAAAAA"),
        (store, "я"),
        (store, ""),
        (another_store, ciphertext),
    ):
        with pytest.raises(SourceSecretError) as error:
            reader.decrypt("alice", "dataset-a", value)
        assert DSN not in str(error.value)
        assert "private-password" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 2},
        {"version": 1, "kind": "postgresql", "owner_id": "alice", "dataset_id": "dataset-a"},
        {"dsn": DSN, "tables": []},
        {"dsn": DSN, "tables": [2]},
        {"dsn": DSN, "tables": "sales"},
    ],
)
def test_authenticated_but_invalid_payload_is_rejected(payload) -> None:
    key = Fernet.generate_key()
    if isinstance(payload, dict):
        payload = {
            "version": 1,
            "kind": "postgresql",
            "owner_id": "alice",
            "dataset_id": "dataset-a",
            **payload,
        }
    ciphertext = Fernet(key).encrypt(json.dumps(payload).encode()).decode()
    with pytest.raises(SourceSecretError):
        SourceSecrets(key.decode()).decrypt("alice", "dataset-a", ciphertext)


MODEL_KEY = "sk-model-private-key"


def test_model_key_round_trip_hides_plaintext() -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    ciphertext = store.encrypt_model_key(MODEL_KEY)
    assert MODEL_KEY not in ciphertext
    assert ciphertext != store.encrypt_model_key(MODEL_KEY)
    assert store.decrypt_model_key(ciphertext) == MODEL_KEY


def test_model_key_and_source_ciphertexts_are_not_interchangeable() -> None:
    store = SourceSecrets(Fernet.generate_key().decode())
    with pytest.raises(SourceSecretError):
        store.decrypt_model_key(store.encrypt("alice", "dataset-a", DSN, None))
    with pytest.raises(SourceSecretError):
        store.decrypt("alice", "dataset-a", store.encrypt_model_key(MODEL_KEY))


def test_model_key_requires_server_key_and_fails_closed() -> None:
    with pytest.raises(SourceSecretError):
        SourceSecrets(None).encrypt_model_key(MODEL_KEY)
    ciphertext = SourceSecrets(Fernet.generate_key().decode()).encrypt_model_key(MODEL_KEY)
    with pytest.raises(SourceSecretError) as error:
        SourceSecrets(Fernet.generate_key().decode()).decrypt_model_key(ciphertext)
    assert MODEL_KEY not in str(error.value)


def test_user_model_keys_are_bound_to_owner_and_not_interchangeable_with_global_keys():
    secrets = SourceSecrets(Fernet.generate_key().decode())
    ciphertext = secrets.encrypt_user_model_key("alice", "user-key-marker")
    assert secrets.decrypt_user_model_key("alice", ciphertext) == "user-key-marker"
    assert "user-key-marker" not in ciphertext
    with pytest.raises(SourceSecretError):
        secrets.decrypt_user_model_key("bob", ciphertext)
    with pytest.raises(SourceSecretError):
        secrets.decrypt_model_key(ciphertext)
    with pytest.raises(SourceSecretError):
        secrets.decrypt_user_model_key("alice", secrets.encrypt_model_key("global-key"))
