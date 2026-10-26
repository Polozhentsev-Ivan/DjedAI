"""Закреплённая ревизия HK_Datasets: манифест и докачка файлов с проверкой Git blob.

Скачать все файлы из корня репозитория:
uv run --locked python scripts/hk_datasets.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "eval/hk_datasets_manifest.json"
CACHE_DIR = ROOT / "artifacts/hk-datasets"
CHUNK_BYTES = 4 * 1024 * 1024


class DownloadError(RuntimeError):
    """Файл не скачан или не совпал с закреплённой ревизией."""


@dataclass(frozen=True)
class HkFile:
    name: str
    size: int
    blob: str
    url: str


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_files(manifest: dict[str, Any]) -> dict[str, HkFile]:
    files = {}
    for table in manifest["tables"]:
        url = manifest["raw_url"].format(revision=manifest["revision"], file=quote(table["file"]))
        files[table["file"]] = HkFile(table["file"], table["size"], table["blob"], url)
    return files


def git_blob(path: Path, size: int) -> str:
    digest = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(file: HkFile, cache: Path = CACHE_DIR) -> Path:
    """Скачивает закреплённый Git blob; докачка частями переживает обрывы GitVerse."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / file.name
    partial = target.with_suffix(".csv.part")
    if not target.exists():
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            offset = partial.stat().st_size if partial.exists() else 0
            while offset < file.size:
                end = min(offset + CHUNK_BYTES, file.size) - 1
                response = client.get(file.url, headers={"Range": f"bytes={offset}-{end}"})
                response.raise_for_status()
                if response.status_code != 206:
                    raise DownloadError("GitVerse не поддержал докачку по Range")
                if response.headers.get("content-range") != f"bytes {offset}-{end}/{file.size}":
                    raise DownloadError("GitVerse вернул другой диапазон или размер файла")
                if len(response.content) != end - offset + 1:
                    raise DownloadError(f"Неполный фрагмент {file.name}")
                with partial.open("ab") as output:
                    output.write(response.content)
                offset = end + 1
                print(f"{file.name}: {offset}/{file.size} bytes", flush=True)
        partial.replace(target)
    if target.stat().st_size != file.size:
        raise DownloadError(f"Повреждён кэш CSV: {target}")
    # Git blob hash also protects an existing cache from stale or altered source data.
    if git_blob(target, file.size) != file.blob:
        raise DownloadError(f"Не совпал Git blob CSV: {target}")
    return target.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Скачать закреплённые файлы HK_Datasets.")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("files", nargs="*", help="имена файлов; по умолчанию все")
    args = parser.parse_args()
    files = manifest_files(load_manifest())
    for name in args.files or files:
        print(download(files[name], args.cache), flush=True)


if __name__ == "__main__":
    main()
