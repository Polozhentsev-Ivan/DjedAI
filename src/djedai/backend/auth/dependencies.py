"""Verified Bearer context shared by AUTH-2 and future resource guards."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from djedai.backend.auth.storage import get_session_user
from djedai.backend.db.models import UserModel
from djedai.backend.db.session import get_db

bearer = HTTPBearer(scheme_name="BearerSession", auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(
        401, "Требуется действующая сессия", headers={"WWW-Authenticate": "Bearer"}
    )


def session_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str:
    """Accept one header with an opaque token; cookies and URL parameters are ignored."""
    if (
        credentials is None
        or sum(name.lower() == b"authorization" for name, _ in request.scope["headers"]) != 1
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", credentials.credentials) is None
    ):
        raise unauthorized()
    return credentials.credentials


def get_current_user(
    token: Annotated[str, Depends(session_token)],
    db: Annotated[Session, Depends(get_db)],
) -> UserModel:
    """Recheck expiry, revocation and account status on each authenticated request."""
    try:
        user = get_session_user(db, token)
    except SQLAlchemyError:
        raise HTTPException(503, "Хранилище сессий недоступно") from None
    if user is None:
        raise unauthorized()
    return user


def current_owner_id(user: Annotated[UserModel, Depends(get_current_user)]) -> str:
    """Owner context for AUTH-3; never reads a client-supplied user_id."""
    return str(user.id)
