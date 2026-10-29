"""Разбор multipart загрузки CSV с освобождением временных файлов при нехватке места (#206)."""

from __future__ import annotations

import errno
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

OUT_OF_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


def is_out_of_space(exc: BaseException) -> bool:
    """Есть ли в цепочке исключения ``OSError`` о нехватке места на диске или квоты."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in OUT_OF_SPACE_ERRNOS:
            return True
        current = current.__cause__ or current.__context__
    return False


class SafeMultiPartParser(MultiPartParser):
    """MultiPartParser, который при ошибке закрывает каждый временный файл.

    Starlette закрывает файлы одним циклом: при ENOSPC ``close()`` первого файла
    падает на flush, и остальные удалённые временные файлы держат место в TMPDIR
    до перезапуска процесса.
    """

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException as exc:
            # Приватный список Starlette; версия закреплена в pyproject.toml.
            for file in self._files_to_close_on_error:
                try:
                    file.close()
                except Exception:
                    pass
            if is_out_of_space(exc):
                raise HTTPException(
                    status_code=507,
                    detail=(
                        "Недостаточно места для загрузки: освободите место во временном "
                        "каталоге (TMPDIR) или загрузите файлы частями"
                    ),
                ) from exc
            raise


class UploadRoute(APIRoute):
    """Маршрут загрузки, который разбирает multipart через SafeMultiPartParser.

    Форма кладётся в кэш запроса до обработчика FastAPI, поэтому сигнатура
    эндпоинта и OpenAPI-схема не меняются, а FastAPI сам закрывает файлы после ответа.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def upload_handler(request: Request) -> Response:
            content_type, _ = parse_options_header(request.headers.get("Content-Type"))
            if content_type == b"multipart/form-data" and request._form is None:
                parser = SafeMultiPartParser(request.headers, request.stream())
                try:
                    request._form = await parser.parse()
                except MultiPartException as exc:
                    raise HTTPException(status_code=400, detail=exc.message) from exc
            return await handler(request)

        return upload_handler
