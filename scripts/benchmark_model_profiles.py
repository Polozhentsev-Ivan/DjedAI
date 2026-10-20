"""Повторяемый замер схем агента и SQL на управляемом API без пользовательских данных."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from djedai.agent.client import BIModelClient  # noqa: E402
from djedai.agent.prompts import load_prompt  # noqa: E402
from djedai.analytics.registry import execute_tool  # noqa: E402
from djedai.contracts.agent import ComposeResponse, PlanResponse, Understanding  # noqa: E402

CATALOG = "Таблица orders: region VARCHAR, amount DECIMAL. Строки: A/10, A/20, B/5."
CASES = [
    (
        "understand_count",
        Understanding,
        "Определи intent по вопросу и каталогу. Запрос конкретного числового показателя "
        "имеет intent=data_question; общий обзор — data_overview. " + CATALOG,
        "Сколько строк в orders?",
        "count",
    ),
    (
        "understand_ambiguous",
        Understanding,
        "Если метрика не определена, intent=ambiguous и clarification с вариантами. "
        "В таблице sales есть gross_sales и net_sales; ни одна не выбрана по умолчанию.",
        "Какие у нас продажи?",
        "ambiguous",
    ),
    (
        "plan_count",
        PlanResponse,
        "Составь план только из run_sql. Он принимает sql. Используй таблицу orders. " + CATALOG,
        "Подсчитай COUNT(*) всех строк orders.",
        "count",
    ),
    (
        "plan_group",
        PlanResponse,
        "Составь план только из run_sql. Он принимает sql. " + CATALOG,
        "Сумма amount по region в orders. Сортировка по region по возрастанию.",
        "group",
    ),
    (
        "compose_count",
        ComposeResponse,
        load_prompt("compose").format(facts="run_sql: columns=[n], rows=[[3]]"),
        "Сколько строк? Ответь только числом 3 в одном текстовом блоке.",
        "count",
    ),
    (
        "compose_empty",
        ComposeResponse,
        load_prompt("compose").format(facts="Нет успешных результатов; данных нет."),
        "Сообщи об отсутствии данных, не придумывая числа.",
        "empty",
    ),
]


def semantic_ok(data, expected: str) -> bool:
    if isinstance(data, Understanding):
        if expected == "ambiguous":
            return data.intent == "ambiguous" and data.clarification is not None
        return data.intent == "data_question"
    if isinstance(data, PlanResponse):
        wanted = [[3]] if expected == "count" else [["A", 30], ["B", 5]]
        with duckdb.connect() as connection:
            connection.execute("CREATE TABLE orders(region VARCHAR, amount DECIMAL(12,2))")
            connection.execute("INSERT INTO orders VALUES ('A',10),('A',20),('B',5)")
            try:
                results = [
                    execute_tool(step.tool, step.args, connection=connection) for step in data.plan
                ]
            except ValueError:
                return False
        return any(
            result.status == "ok" and result.data is not None and result.data.rows == wanted
            for result in results
        )
    if isinstance(data, ComposeResponse):
        text = "\n".join(block.markdown or "" for block in data.answer_blocks).strip()
        if expected == "count":
            return text.strip("`* ") == "3" and all(b.type == "text" for b in data.answer_blocks)
        return bool(text) and not any(char.isdigit() for char in text)
    return False


def run_benchmark(client: BIModelClient, *, repeats: int, context_lines: int) -> dict:
    rows = []
    cases = [(case, turn) for turn in range(repeats) for case in CASES]
    if context_lines:
        padding = "\n".join(
            f"Служебная таблица extra_{n}: id VARCHAR, label VARCHAR." for n in range(context_lines)
        )
        cases.append(
            (
                (
                    "context_probe",
                    Understanding,
                    "Определи intent вопроса о таблице orders. Запрос конкретного числового "
                    "показателя имеет intent=data_question.\n" + padding + "\n" + CATALOG,
                    "Сколько строк в orders?",
                    "count",
                ),
                0,
            )
        )
    for (name, schema, system, question, expected), turn in cases:
        started = perf_counter()
        response = client.generate_typed_response(system, question, schema)
        elapsed = perf_counter() - started
        valid = response.status == "success" and response.data is not None
        correct = semantic_ok(response.data, expected) if valid else False
        rows.append(
            {
                "case": name,
                "repeat": turn + 1,
                "schema": schema.__name__,
                "seconds": round(elapsed, 3),
                "schema_valid": valid,
                "semantic_ok": correct,
                "error_type": response.error_type,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "finish_reason": response.finish_reason,
                "data": response.data.model_dump(mode="json") if valid else None,
            }
        )
        print(
            f"{client.model}: {name} {turn + 1}: schema={valid} semantic={correct} {elapsed:.2f}s",
            flush=True,
        )
    durations = sorted(row["seconds"] for row in rows)
    return {
        "model": client.model,
        "requests": len(rows),
        "schema_valid": sum(row["schema_valid"] for row in rows),
        "semantic_ok": sum(row["semantic_ok"] for row in rows),
        "latency_seconds": {
            "median": statistics.median(durations),
            "p95": durations[max(0, (95 * len(rows) + 99) // 100 - 1)],
            "max": max(durations),
        },
        "max_prompt_tokens": max((row["prompt_tokens"] or 0) for row in rows),
        "calls": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--base-url", default="https://api.deepinfra.com/v1/openai")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--context-lines", type=int, default=200)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/model-profiles" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json",
    )
    args = parser.parse_args(argv)
    if (
        args.repeats < 1
        or args.context_lines < 0
        or args.max_output_tokens < 1
        or args.timeout <= 0
    ):
        parser.error("Количество повторов, лимит ответа и timeout должны быть положительными")
    values = {**dotenv_values(args.env_file), **os.environ}
    key = values.get("DEEPINFRA_API_KEY") or values.get("MODEL_API_KEY")
    if not key:
        parser.error("Не найден ключ модели в окружении или .env")
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout,
        "models": [],
    }
    try:
        for model in args.model:
            client = BIModelClient(
                api_key=key,
                base_url=args.base_url,
                model_name=model,
                timeout=args.timeout,
                max_output_tokens=args.max_output_tokens,
            )
            try:
                report["models"].append(
                    run_benchmark(client, repeats=args.repeats, context_lines=args.context_lines)
                )
            finally:
                client.client.close()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return 0
    except Exception as error:
        print(f"Benchmark failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
