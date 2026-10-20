"""Хранение учётных записей и серверных сессий (AUTH-1).

Функции записи фиксируют всю текущую транзакцию переданной SQLAlchemy Session,
как существующие репозитории приложения. Для одной операции рекомендуется
отдельная Session. Ошибки БД (в том числе занятый username) передаются вызывающему
коду: перед повторным использованием Session после такой ошибки нужен rollback.
Функции чтения не фиксируют транзакции. HTTP, cookie и JWT относятся к AUTH-2.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from djedai.backend.auth.passwords import hash_password, verify_password
from djedai.backend.db.models import SessionModel, UserModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(db: Session, username: str, password: str, email: str | None = None) -> UserModel:
    """Создаёт активного пользователя; username уникален с учётом регистра.

    Логин содержит 3..50 символов и не состоит только из пробелов. Пароль
    проходит проверку hash_password. Исходные логин и пароль не нормализуются.
    Ошибка уникальности передаётся как sqlalchemy.exc.IntegrityError.
    """
    if not isinstance(username, str) or not 3 <= len(username) <= 50 or not username.strip():
        raise ValueError("Логин должен содержать от 3 до 50 символов и не быть пустым")
    if email is not None and (not isinstance(email, str) or len(email) > 255):
        raise ValueError("Email должен содержать не более 255 символов")
    user = UserModel(
        id=uuid4().hex,
        username=username,
        email=email,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, username: str, password: str) -> UserModel | None:
    """Возвращает активного пользователя с верным паролем, иначе None."""
    user = db.scalar(
        select(UserModel).where(UserModel.username == username, UserModel.is_active.is_(True))
    )
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def create_session(db: Session, user_id: str, *, ttl: timedelta = timedelta(days=1)) -> str:
    """Создаёт сессию и однократно возвращает секретный токен из 256 случайных бит.

    В БД сохраняется только SHA-256 токена. Пользователь должен быть активным
    и иметь хеш пароля; срок жизни должен быть положительным. Иначе — ValueError.
    """
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ValueError("Срок жизни сессии должен быть положительным timedelta")
    created_at = _utc_now()
    try:
        expires_at = created_at + ttl
    except OverflowError as exc:
        raise ValueError("Срок жизни сессии выходит за допустимый диапазон дат") from exc
    user = db.scalar(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.is_active.is_(True),
            UserModel.password_hash.is_not(None),
            UserModel.password_hash != "",
        )
    )
    if user is None:
        raise ValueError("Для сессии нужен существующий активный пользователь с паролем")
    token = secrets.token_urlsafe(32)
    record = SessionModel(
        token_hash=_token_hash(token),
        user_id=user_id,
        created_at=created_at,
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    return token


def get_session_user(db: Session, token: str) -> UserModel | None:
    """Возвращает владельца действующей сессии; неизвестный токен даёт None."""
    if not isinstance(token, str) or not token:
        return None
    return db.scalar(
        select(UserModel)
        .join(SessionModel, SessionModel.user_id == UserModel.id)
        .where(
            SessionModel.token_hash == _token_hash(token),
            SessionModel.revoked_at.is_(None),
            SessionModel.expires_at > _utc_now(),
            UserModel.is_active.is_(True),
        )
    )


def revoke_session(db: Session, token: str) -> bool:
    """Отзывает сессию, включая истёкшую; True означает первый успешный отзыв."""
    if not isinstance(token, str) or not token:
        return False
    result = db.execute(
        update(SessionModel)
        .where(SessionModel.token_hash == _token_hash(token), SessionModel.revoked_at.is_(None))
        .values(revoked_at=_utc_now())
    )
    db.commit()
    return result.rowcount > 0
