"""Хеширование паролей Argon2id без изменения исходного пароля."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# 64 MiB памяти и три прохода; соль генерируется отдельно для каждого пароля.
_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_MAX_PASSWORD_LENGTH = 1_024
# A missing/disabled account must still perform password verification work.
# This hash is never stored on an account and cannot authenticate anyone.
_DUMMY_HASH = _password_hasher.hash("authentication-timing-placeholder")


def _dummy_verify(password: str) -> None:
    try:
        _password_hasher.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, UnicodeError):
        pass


def hash_password(password: str) -> str:
    """Возвращает Argon2id-хеш со случайной солью; длина пароля — 6..1024 символа.

    Пароль не обрезается и не нормализуется. Для некорректного значения
    поднимает ValueError; исходный пароль в результате не сохраняется.
    """
    if not isinstance(password, str) or not 6 <= len(password) <= _MAX_PASSWORD_LENGTH:
        raise ValueError("Пароль должен содержать от 6 до 1024 символов")
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Проверяет пароль; пустой, повреждённый или неподходящий хеш даёт False."""
    if not isinstance(password, str) or len(password) > _MAX_PASSWORD_LENGTH:
        return False
    if not isinstance(password_hash, str) or not password_hash:
        _dummy_verify(password)
        return False
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError, UnicodeError):
        _dummy_verify(password)
        return False
