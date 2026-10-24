"""Real CP1 backend with an isolated, deterministic OpenAI-compatible model server.

Used only by Playwright. Production runtime, SQL tools and storage are unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from djedai.backend.app import create_app  # noqa: E402
from djedai.backend.config import Settings  # noqa: E402


def model_app() -> FastAPI:
    app = FastAPI()
    controls: dict[str, dict] = {}

    @app.post("/control")
    def control(payload: dict) -> dict:
        key = f"{payload['marker']}:{payload['stage']}"
        if payload.get("release"):
            controls[key]["gate"].set()
        else:
            controls[key] = {
                **payload,
                "gate": threading.Event(),
                "started": 0,
                "finished": 0,
            }
        return {"ok": True}

    @app.get("/control")
    def observations() -> dict:
        return {
            key: {"started": value["started"], "finished": value["finished"]}
            for key, value in controls.items()
        }

    @app.post("/v1/chat/completions", response_model=None)
    def complete(payload: dict) -> dict | JSONResponse:
        name = payload["response_format"]["json_schema"]["name"]
        system, question = [entry["content"] for entry in payload["messages"]]
        for value in list(controls.values()):
            if (value["marker"] != "*" and value["marker"] not in question) or value[
                "stage"
            ] != name:
                continue
            value["started"] += 1
            mode = value["mode"]
            if mode == "gate":
                value["gate"].wait(10)
            elif mode == "timeout":
                time.sleep(12)
            elif mode == "unavailable" or (mode == "retry" and value["started"] == 1):
                return JSONResponse(
                    {"error": {"message": "Controlled unavailable"}}, status_code=503
                )
            value["finished"] += 1
        if "ошибка модели" in question.lower():
            return {"choices": [{"finish_reason": "stop", "message": {"content": "invalid"}}]}
        if "медленно" in question.lower():
            time.sleep(1)
        if name == "Understanding":
            value = {"intent": "data_question", "resolved_question": question}
        elif name == "PlanResponse":
            table = re.search(r"^Таблица (\w+)", system, re.MULTILINE).group(1)
            value = {
                "plan": [
                    {
                        "tool": "run_sql",
                        "args": {
                            "sql": f'SELECT COUNT(*) AS n, SUM(amount) AS total FROM "{table}"'
                        },
                    }
                ]
            }
        elif name == "ComposeResponse":
            row = re.search(r"rows=\[\[([^\]]+)\]\]", system).group(1)
            numbers = re.findall(r"-?\d+(?:\.\d+)?", row)
            markdown = (
                f"COUNT={numbers[0]};SUM={numbers[1]}"
                if "COUNT=" in question
                else f"Строк: {numbers[0]}; сумма: {numbers[1]}."
            )
            value = {"answer_blocks": [{"type": "text", "markdown": markdown}]}
        elif name == "VerifyResponse":
            draft = re.search(r"Drafted Response:\n(.*?)\n\nRules:", system, re.DOTALL).group(1)
            value = {"is_valid": True, "issues": [], "corrected_markdown": draft}
        else:
            raise ValueError(f"Unexpected response schema: {name}")
        return {
            "id": "cp1-controlled-model",
            "object": "chat.completion",
            "model": "cp1-controlled-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(value, ensure_ascii=False),
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--model-port", type=int, default=18081)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        _env_file=None,
        app_env="test",
        cp1_local_mode=False,
        app_database_url=f"sqlite:///{directory / 'app.db'}",
        datasets_data_dir=str(directory / "datasets"),
        results_data_dir=str(directory / "results"),
        # The browser suite logs many isolated accounts in from the same loopback IP.
        auth_rate_limit_requests=100,
    )
    # The real client reads its existing environment contract, no runtime injection.
    import os

    os.environ.update(
        {
            "MODEL_BASE_URL": f"http://127.0.0.1:{args.model_port}/v1",
            "MODEL_NAME": "cp1-controlled-model",
            "MODEL_API_KEY": "cp1-test-only",
            "MODEL_TIMEOUT_SECONDS": "5",
            "MODEL_RETRY_DELAY_SECONDS": "0.6",
        }
    )
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(migration, "head")
    model = uvicorn.Server(uvicorn.Config(model_app(), host="127.0.0.1", port=args.model_port))
    worker = threading.Thread(target=model.run, daemon=True)
    worker.start()
    try:
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
    finally:
        model.should_exit = True
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
