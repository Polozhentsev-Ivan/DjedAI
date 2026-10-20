"""Хеширование паролей Argon2id без изменения исходного пароля."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
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
    if (
        not isinstance(password, str)
        or len(password) > _MAX_PASSWORD_LENGTH
        or not isinstance(password_hash, str)
        or not password_hash
    ):
        return False
    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, UnicodeError):
        return False
