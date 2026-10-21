"""Explicit single-user CP1 context and idempotent local-user bootstrap.

This adapter is deliberately disabled by default. It does not authenticate HTTP
credentials and must be replaced by the agreed session dependency in AUTH-2.
Run migrations, enable CP1_LOCAL_MODE and execute
``uv run --locked python -m djedai.backend.bootstrap_local_user`` before starting CP1.
"""

from __future__ import annotations

import argparse
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.config import get_settings
from djedai.backend.db.models import UserModel
from djedai.backend.db.session import create_db_engine, get_db


def get_local_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserModel:
    """Resolve the server-configured owner; client user IDs are never accepted."""
    settings = request.app.state.settings
    if not settings.cp1_local_mode:
        raise HTTPException(status_code=503, detail="Локальный режим CP1 не включён")
    user = db.get(UserModel, settings.cp1_local_user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=503, detail="Локальный пользователь CP1 не подготовлен")
    return user


def local_owner_id(user: Annotated[UserModel, Depends(get_local_user)]) -> str:
    return str(user.id)


def bootstrap_local_user(
    factory: sessionmaker[Session], user_id: str, *, username: str = "cp1-local"
) -> str:
    """Create the configured real owner once, preserving all existing resources."""
    with factory() as db:
        existing = db.get(UserModel, user_id)
        if existing is not None:
            if not existing.is_active:
                raise ValueError("Настроенный пользователь неактивен")
            return str(existing.id)
        if db.scalar(select(UserModel).where(UserModel.username == username)) is not None:
            raise ValueError("Имя пользователя уже занято другим ID")
        db.add(UserModel(id=user_id, username=username, is_active=True))
        db.commit()
    return user_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Подготовить владельца локального режима CP1")
    parser.add_argument("--username", default="cp1-local")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.cp1_local_mode:
        parser.error("Установите CP1_LOCAL_MODE=true для однопользовательского режима")
    engine = create_db_engine(settings.app_database_url)
    try:
        user_id = bootstrap_local_user(
            sessionmaker(bind=engine), settings.cp1_local_user_id, username=args.username
        )
        print(f"CP1 local owner ready: {user_id}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
