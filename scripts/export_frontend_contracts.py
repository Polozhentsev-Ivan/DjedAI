#!/usr/bin/env python3
"""Export the shared contracts consumed by the frontend snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from export_schema import build_openapi_schema, get_component_refs

OUTPUT = Path(__file__).resolve().parents[1] / "frontend/schema/contracts.json"
CONTRACTS = {
    "DatasetCatalog",
    "DatasetSummary",
    "DatasetDetail",
    "ChartSpec",
    "AssistantMessage",
    "ChatSummary",
    "ChatMessage",
    "ChatDetail",
    "MessageBlock",
    "Dashboard",
    "DashboardSnapshot",
    "UserRead",
    "TokenResponse",
    "ErrorResponse",
    "Transformation",
    "ToolResult",
    "ToolResultData",
    "DatasetProgressData",
    "DatasetDoneData",
    "SSEStatusData",
    "SSEToolCallData",
    "SSEToolResultData",
    "SSEDoneData",
    "SSEErrorData",
}


def export() -> str:
    """Keep canonical definitions and all their transitive references intact."""
    schema = build_openapi_schema()
    available = schema["components"]["schemas"]
    selected: dict = {}
    pending = set(CONTRACTS)
    while pending:
        name = pending.pop()
        selected[name] = available[name]
        pending.update(get_component_refs(available[name]) - selected.keys())
    snapshot = {
        "openapi": schema["openapi"],
        "info": {"title": "DjedAI shared frontend contracts", "version": schema["info"]["version"]},
        "paths": {},
        "components": {"schemas": {name: selected[name] for name in sorted(selected)}},
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the snapshot is stale")
    args = parser.parse_args()
    expected = export()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != expected:
            print(
                "Frontend contracts are stale. "
                "Run: uv run python scripts/export_frontend_contracts.py"
            )
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
