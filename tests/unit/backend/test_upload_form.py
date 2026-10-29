"""Разбор multipart при нехватке места во временном каталоге (#206)."""

from __future__ import annotations

import errno
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from djedai.backend.upload_form import SafeMultiPartParser, is_out_of_space

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fake_full_disk import FullDiskFile, install_full_disk  # noqa: E402

BOUNDARY = "djedai-boundary"


@pytest.fixture
def full_disk(monkeypatch: pytest.MonkeyPatch) -> type[FullDiskFile]:
    return install_full_disk(monkeypatch, capacity=3000)


def _multipart(files: list[tuple[str, bytes]]) -> bytes:
    body = b""
    for name, content in files:
        body += (
            (
                f"--{BOUNDARY}\r\n"
                f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    return body + f"--{BOUNDARY}--\r\n".encode()


def _parser(body: bytes) -> SafeMultiPartParser:
    async def stream() -> AsyncIterator[bytes]:
        for start in range(0, len(body), 512):
            yield body[start : start + 512]

    headers = Headers({"content-type": f"multipart/form-data; boundary={BOUNDARY}"})
    return SafeMultiPartParser(headers, stream())


def _three_files() -> bytes:
    return _multipart([(f"part{index}.csv", b"a,b\n" + b"1,2\n" * 400) for index in range(3)])


@pytest.mark.anyio
async def test_out_of_space_closes_every_temp_file_and_reports_507(full_disk) -> None:
    with pytest.raises(HTTPException) as caught:
        await _parser(_three_files()).parse()

    assert caught.value.status_code == 507
    assert "Недостаточно места для загрузки" in caught.value.detail
    assert len(full_disk.created) == 3
    assert all(file.closed for file in full_disk.created)


@pytest.mark.anyio
async def test_other_os_errors_close_files_and_propagate(full_disk) -> None:
    full_disk.error = errno.EIO

    with pytest.raises(OSError) as caught:
        await _parser(_three_files()).parse()

    assert caught.value.errno == errno.EIO
    assert full_disk.created
    assert all(file.closed for file in full_disk.created)


@pytest.mark.anyio
async def test_successful_parse_keeps_files_open(full_disk) -> None:
    full_disk.capacity = 1 << 20

    form = await _parser(_three_files()).parse()

    assert len(form.getlist("files")) == 3
    assert not any(file.closed for file in full_disk.created)
    await form.close()


def test_is_out_of_space_follows_exception_chain() -> None:
    try:
        try:
            raise OSError(errno.EDQUOT, "Disk quota exceeded")
        except OSError as exc:
            raise RuntimeError("wrapped") from exc
    except RuntimeError as wrapped:
        assert is_out_of_space(wrapped)
    assert not is_out_of_space(OSError(errno.EIO, "I/O error"))
    assert not is_out_of_space(ValueError("bad"))
