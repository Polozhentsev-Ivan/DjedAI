"""Authenticated encryption for owned external source credentials.

Only ciphertext belongs in application storage. The server key stays in settings;
the authenticated payload also binds a source to its immutable owner, dataset and kind,
so a PostgreSQL secret cannot be replayed as a ClickHouse source or the other way round.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

SOURCE_KINDS = ("postgresql", "clickhouse")


class SourceSecretError(ValueError):
    """A source cannot be decrypted; its details must never be exposed."""


@dataclass(frozen=True)
class SourceSecret:
    dsn: str = field(repr=False)
    tables: tuple[str, ...] | None = None


PostgresSecret = SourceSecret


class SourceSecrets:
    def __init__(self, key: str | None) -> None:
        self._cipher: Fernet | None = None
        if key:
            try:
                self._cipher = Fernet(key.encode("ascii"))
            except (ValueError, UnicodeError):
                pass

    def require_key(self) -> None:
        if self._cipher is None:
            raise SourceSecretError("Ключ шифрования источников не настроен")

    def encrypt(
        self,
        owner_id: str,
        dataset_id: str,
        dsn: str,
        tables: Sequence[str] | None,
        *,
        kind: str = "postgresql",
    ) -> str:
        self.require_key()
        if kind not in SOURCE_KINDS:
            raise SourceSecretError("Неподдерживаемый источник")
        payload = json.dumps(
            {
                "version": 1,
                "kind": kind,
                "owner_id": owner_id,
                "dataset_id": dataset_id,
                "dsn": dsn,
                "tables": list(tables) if tables is not None else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert self._cipher is not None
        return self._cipher.encrypt(payload).decode("ascii")

    def decrypt(
        self, owner_id: str, dataset_id: str, ciphertext: str, *, kind: str = "postgresql"
    ) -> SourceSecret:
        self.require_key()
        assert self._cipher is not None
        try:
            payload = json.loads(self._cipher.decrypt(ciphertext.encode("ascii")))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("kind") != kind
                or payload.get("owner_id") != owner_id
                or payload.get("dataset_id") != dataset_id
                or not isinstance(payload.get("dsn"), str)
                or not payload["dsn"]
            ):
                raise ValueError
            tables = payload.get("tables")
            if tables is not None and (
                not isinstance(tables, list)
                or not tables
                or any(not isinstance(table, str) or not table for table in tables)
            ):
                raise ValueError
            return SourceSecret(payload["dsn"], tuple(tables) if tables is not None else None)
        except (InvalidToken, ValueError, TypeError, UnicodeError, KeyError):
            raise SourceSecretError("Не удалось открыть защищённый источник") from None
