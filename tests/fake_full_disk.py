"""Временные файлы multipart на «диске» ограниченного объёма (#206).

Подменяет ``SpooledTemporaryFile`` в разборе форм Starlette: запись сверх ёмкости
падает с заданным errno, а ``close()`` такого файла повторяет ошибку, как flush
буферизованного файла при ENOSPC.
"""

from __future__ import annotations

import errno
import io

import pytest
from starlette import formparsers


class FullDiskFile(io.BytesIO):
    created: list[FullDiskFile] = []
    capacity = 0
    used = 0
    error = errno.ENOSPC

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__()
        self.pending = False
        FullDiskFile.created.append(self)

    def write(self, data: bytes) -> int:  # type: ignore[override]
        if FullDiskFile.used + len(data) > FullDiskFile.capacity:
            self.pending = True
            raise OSError(FullDiskFile.error, "No space left on device")
        FullDiskFile.used += len(data)
        return super().write(data)

    def close(self) -> None:
        super().close()
        if self.pending:
            self.pending = False
            raise OSError(FullDiskFile.error, "No space left on device")


def install_full_disk(
    monkeypatch: pytest.MonkeyPatch, capacity: int, error: int = errno.ENOSPC
) -> type[FullDiskFile]:
    FullDiskFile.created = []
    FullDiskFile.used = 0
    FullDiskFile.capacity = capacity
    FullDiskFile.error = error
    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", FullDiskFile)
    return FullDiskFile
