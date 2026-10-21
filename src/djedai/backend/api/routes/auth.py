"""Local accounts and revocable, opaque Bearer sessions (AUTH-2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from djedai.backend.auth.dependencies import get_current_user, session_token
from djedai.backend.auth.storage import (
    authenticate_user,
    create_session,
    create_user,
    revoke_session,
)
from djedai.backend.db.models import UserModel
from djedai.backend.db.session import get_db
from djedai.contracts.auth import TokenResponse, UserCreate, UserLogin, UserRead
from djedai.contracts.common import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"], responses={503: {"model": ErrorResponse}})
_unauthorized = {401: {"model": ErrorResponse}}
_auth_input_errors = {code: {"model": ErrorResponse} for code in (408, 413, 429)}
Database = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[UserModel, Depends(get_current_user)]


def read_user(user: UserModel) -> UserRead:
    return UserRead.model_validate(user, from_attributes=True)


@router.post(
    "/register",
    response_model=UserRead,
    status_code=201,
    responses={409: {"model": ErrorResponse}, **_auth_input_errors},
)
def register(user_in: UserCreate, db: Database) -> UserRead:
    """Create an account without automatically creating or replacing a session."""
    try:
        user = create_user(db, user_in.username, user_in.password, user_in.email)
    except IntegrityError:
        db.rollback()
        # Only an actual username conflict is a 409; other DB failures are unavailable.
        try:
            duplicate = db.scalar(
                select(UserModel.id).where(UserModel.username == user_in.username)
            )
        except SQLAlchemyError:
            raise HTTPException(503, "Хранилище пользователей недоступно") from None
        if duplicate:
            raise HTTPException(409, "Пользователь с таким именем уже существует") from None
        raise HTTPException(503, "Хранилище пользователей недоступно") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Хранилище пользователей недоступно") from None
    return read_user(user)


@router.post(
    "/login", response_model=TokenResponse, responses={**_unauthorized, **_auth_input_errors}
)
def login(credentials: UserLogin, db: Database) -> TokenResponse:
    """Return a fresh token once; never disclose why authentication failed."""
    try:
        user = authenticate_user(db, credentials.username, credentials.password)
        if user is None:
            raise HTTPException(
                401, "Неверный логин или пароль", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            token = create_session(db, str(user.id))
        except ValueError:
            # Account state can change between checking the password and creating a session.
            raise HTTPException(
                401, "Неверный логин или пароль", headers={"WWW-Authenticate": "Bearer"}
            ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Хранилище сессий недоступно") from None
    return TokenResponse(access_token=token, expires_in=86400)


@router.get("/me", response_model=UserRead, responses=_unauthorized)
def me(user: CurrentUser) -> UserRead:
    return read_user(user)


@router.post("/logout", status_code=204, responses=_unauthorized)
def logout(
    user: CurrentUser, token: Annotated[str, Depends(session_token)], db: Database
) -> Response:
    """Revoke only the authenticated session; other sessions remain valid."""
    try:
        revoke_session(db, token)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Хранилище сессий недоступно") from None
    return Response(status_code=204)
