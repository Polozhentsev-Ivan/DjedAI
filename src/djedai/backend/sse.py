"""Кодирование событий агента в wire-формат Server-Sent Events."""

from __future__ import annotations

import json

from djedai.contracts.sse import AgentStreamEvent, DatasetStreamEvent


def encode_sse_event(event: AgentStreamEvent | DatasetStreamEvent) -> str:
    """Собирает SSE-кадр из события: `event: <name>` и JSON в поле `data`."""
    payload = event.model_dump(mode="json")
    data = json.dumps(payload["data"], ensure_ascii=False, separators=(",", ":"))
    return f"event: {payload['event']}\ndata: {data}\n\n"
