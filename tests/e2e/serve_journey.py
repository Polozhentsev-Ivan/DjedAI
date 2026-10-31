"""Strict HTTP model fixture for #175, never a replacement for runtime APIs or SQL.

Only selects queries and charts. All numbers and result IDs come from the real
tools; follow-ups fail closed when the runtime omits the required chat context.
"""

from __future__ import annotations

import json
import re
import threading

from fastapi import FastAPI


def json_after(text: str, marker: str):
    return json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())[0]


def focus_id(system: str) -> str:
    match = re.search(r"^1\. result_id=([^;\s]+);", system, re.MULTILINE)
    if not match:
        raise ValueError("Follow-up received no current-chat focus result")
    return match[1]


def table_id(system: str, suffix: str) -> str:
    tables = re.findall(r"^Таблица ([\w.]+)", system, re.MULTILINE)
    matches = [name for name in tables if name.split(".")[-1] == suffix]
    if len(matches) != 1:
        raise ValueError(f"Expected one catalog table ending in {suffix}, got {tables}")
    return '"' + matches[0].replace('"', '""') + '"'


def response_for(name: str, system: str, question: str) -> dict:
    if name == "Understanding":
        if "то же самое по регионам" in question:
            focus_id(system)
            if "январь 2025" not in system:
                raise ValueError("Follow-up lost the period from the previous question")
            return {
                "intent": "visualize",
                "resolved_question": "Покажи сумму amount по region за январь 2025.",
            }
        if "линейным" in question:
            return {
                "intent": "modify_previous",
                "resolved_question": question,
                "references": [focus_id(system)],
            }
        if "category" in question and "Истории предыдущих сообщений нет." not in system:
            raise ValueError("The initial question unexpectedly received another chat's history")
        return {"intent": "visualize", "resolved_question": question}
    if name == "PlanResponse":
        if "линейным" in question:
            focus_id(system)
            return {"plan": []}
        if "target" in question:
            sql = (
                f"SELECT region, SUM(target) AS total FROM {table_id(system, 'targets')} "
                "GROUP BY region ORDER BY region"
            )
        else:
            group = "region" if "region" in question else "category"
            if "январь 2025" not in question:
                raise ValueError("Resolved question lost its explicit period")
            sql = (
                f"SELECT {group}, SUM(amount) AS total FROM {table_id(system, 'orders')} "
                "WHERE ordered_at >= DATE '2025-01-01' AND ordered_at < DATE '2025-02-01' "
                f"GROUP BY {group} ORDER BY {group}"
            )
        return {"plan": [{"tool": "run_sql", "args": {"sql": sql}}]}
    if name == "VisualizeResponse":
        results = json_after(system, "Tool Results (Data & Hints):")
        result = results[-1]
        columns = [c if isinstance(c, str) else c["name"] for c in result["columns"]]
        assert columns[1] == "total", columns
        return {
            "charts": [
                {
                    "chart_id": "journey-chart",
                    "result_id": result["result_id"],
                    "type": "line" if "линейным" in question else "bar",
                    "title": "План по регионам" if "target" in question else "Сумма по группам",
                    "x": {"field": columns[0], "kind": "category"},
                    "y": [{"field": "total", "format": "number"}],
                }
            ]
        }
    if name == "ComposeResponse":
        results = json_after(system, "Facts and Data:")
        result = next(r for r in reversed(results) if r.get("data", {}).get("columns"))
        return {
            "answer_blocks": [
                {"type": "text", "markdown": "Результат расчёта показан в таблице и на графике."},
                {"type": "table", "result_id": result["result_id"]},
            ]
        }
    if name == "VerifyResponse":
        return {"is_valid": True, "issues": [], "corrected_markdown": None}
    raise ValueError(f"Unexpected schema: {name}")


def model_app() -> FastAPI:
    app = FastAPI()
    calls: list[dict] = []
    gate = threading.Event()
    gate.set()

    @app.get("/observations")
    def observations() -> dict:
        return {"calls": calls}

    @app.post("/control")
    def control(payload: dict) -> dict:
        if payload["hold"]:
            gate.clear()
        else:
            gate.set()
        return {"ok": True}

    @app.post("/v1/chat/completions")
    def complete(payload: dict) -> dict:
        name = payload["response_format"]["json_schema"]["name"]
        system, question = [m["content"] for m in payload["messages"]]
        call = {"stage": name, "finished": False}
        calls.append(call)
        if name == "Understanding" and not gate.wait(30):
            raise ValueError("Test failed to release the model gate")
        value = response_for(name, system, question)
        if name == "PlanResponse":
            call["tool_calls"] = len(value["plan"])
        call["finished"] = True
        return {
            "id": "journey-controlled-model",
            "object": "chat.completion",
            "model": "journey-controlled-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(value)},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return app
