"""A verified session lifetime for background workers and already opened streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.auth.dependencies import get_current_user, session_token, unauthorized
from djedai.backend.auth.storage import get_session_user
from djedai.backend.db.models import UserModel
from djedai.backend.db.session import get_session_factory


@dataclass(frozen=True)
class SessionAccess:
    owner_id: str
    _token: str = field(repr=False)
    _factory: sessionmaker[Session] = field(repr=False)

    def valid(self) -> bool:
        """Do not reuse the request's ORM identity cache or an open transaction."""
        try:
            with self._factory() as db:
                user = get_session_user(db, self._token)
                return user is not None and user.id == self.owner_id
        except SQLAlchemyError:
            return False

    def require(self) -> None:
        if not self.valid():
            raise unauthorized()


def get_session_access(
    user: Annotated[UserModel, Depends(get_current_user)],
    token: Annotated[str, Depends(session_token)],
    factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> SessionAccess:
    return SessionAccess(str(user.id), token, factory)


async def guard_stream[T](
    events: AsyncIterator[T], access: SessionAccess, expired_event: T
) -> AsyncIterator[T]:
    """Revalidate even while the source is idle; cancel pending reads on revocation."""
    pending: asyncio.Task[T] | None = None
    try:
        while True:
            if not await asyncio.to_thread(access.valid):
                yield expired_event
                return
            if pending is None:
                pending = asyncio.create_task(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=0.2)
            if not await asyncio.to_thread(access.valid):
                yield expired_event
                return
            if not done:
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        closer = getattr(events, "aclose", None)
        if closer is not None:
            await closer()
