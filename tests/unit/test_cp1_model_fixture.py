"""The browser fixture accepts the production JSON facts, including Decimal strings."""

import json
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("numbers", [[3, 75.5], ["3", "75.50"]])
def test_cp1_model_composes_from_json_facts(numbers):
    fixture = runpy.run_path(str(Path(__file__).parents[1] / "e2e/serve_cp1.py"))
    system = (
        "Facts and Data:\n"
        + json.dumps([{"tool": "run_sql", "data": {"columns": ["n", "total"], "rows": [numbers]}}])
        + "\n\nData policy: use only the provided facts."
    )
    with TestClient(fixture["model_app"]()) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "response_format": {"json_schema": {"name": "ComposeResponse"}},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "COUNT=...;SUM=..."},
                ],
            },
        )
    assert result.status_code == 200
    body = json.loads(result.json()["choices"][0]["message"]["content"])
    assert body["answer_blocks"] == [
        {"type": "text", "markdown": f"COUNT={numbers[0]};SUM={numbers[1]}"}
    ]
