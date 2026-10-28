"""Bounded operator-only JSONL journal; no HTTP route exposes these files."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()
logger = logging.getLogger(__name__)


class DiagnosticJournal:
    def __init__(self, directory: Path | str, max_bytes: int, backups: int) -> None:
        self.path = Path(directory) / "agent.jsonl"
        self.max_bytes = max_bytes
        self.backups = backups

    def __call__(self, event: dict[str, Any]) -> None:
        encoded = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        try:
            with _lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
                        target = Path(f"{self.path}.{index}")
                        if source.exists():
                            source.replace(target)
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "ab") as handle:
                    if self.path.stat().st_size:
                        with self.path.open("rb") as previous:
                            previous.seek(-1, os.SEEK_END)
                            if previous.read(1) != b"\n":
                                handle.write(b"\n")
                    handle.write(encoded)
        except OSError as exc:
            # Storage failure must be visible, but must not log paths or user contents.
            logger.error("Agent diagnostics unavailable (error_type=%s)", type(exc).__name__)


def read_events(
    directory: Path, run_id: str | None = None, event_type: str | None = None
) -> list[dict[str, Any]]:
    """Read retained events locally as the server operator, including rotated files."""
    paths = sorted(
        directory.glob("agent.jsonl.*"),
        key=lambda path: int(path.suffix[1:]) if path.suffix[1:].isdigit() else -1,
        reverse=True,
    ) + [directory / "agent.jsonl"]
    events = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_bytes().splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # An interrupted final write does not hide older complete events.
            if isinstance(event, dict):
                if run_id is not None and event.get("run_id") != run_id:
                    continue
                if event_type is not None and event.get("type") != event_type:
                    continue
                events.append(event)
    return events
