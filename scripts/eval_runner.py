"""Evaluation Runner for the BI Agent.

Runs the golden test set through the agent graph and generates a report detailing
completion status, duration, used tools, and numerical checks.
"""

import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Dynamically add the src directory to PYTHONPATH to fix ModuleNotFoundError
sys.path.insert(0, str(ROOT / "src"))

from djedai.agent.config import get_agent_client  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.contracts.agent import Message  # noqa: E402

DEFAULT_QUESTIONS = ROOT / "eval/questions.yaml"

# Define current agent capabilities.
# Used to mark scenarios as skipped if they require unsupported features.
SUPPORTED_REQUIREMENTS = {"memory"}
SUPPORTED_TOOLS = {"describe_dataset", "describe_column", "value_counts", "run_sql"}


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently losing expectations."""


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def check_fact(fact: dict, answer_text: str, results_data: str) -> bool:
    """Heuristic check to see if the expected numerical value exists in the response or data."""
    expected = fact["value"]

    def flatten(items):
        """Recursively flatten nested lists."""
        for i in items:
            if isinstance(i, list):
                yield from flatten(i)
            else:
                yield i

    if isinstance(expected, list):
        for item in flatten(expected):
            if str(item) not in answer_text and str(item) not in results_data:
                return False
        return True

    # Exact or approx check by string representation existence
    return str(expected) in answer_text or str(expected) in results_data


def process_case(case, client, graph):
    """Обрабатывает один тестовый сценарий (вызывается в отдельном потоке)."""
    case_id = case["id"]
    expect = case.get("expect", {})
    reqs = set(expect.get("requirements", []))

    if not reqs.issubset(SUPPORTED_REQUIREMENTS):
        return {
            "type": "skipped",
            "data": {
                "id": case_id,
                "status": "skipped",
                "reason": f"Unsupported reqs: {list(reqs - SUPPORTED_REQUIREMENTS)}",
            },
        }

    start_time = time.time()
    initial_state = {
        "chat_id": f"eval_{case_id}",
        "dataset_id": case.get("dataset", "ds_1"),
        "messages": [Message(role="user", content=case.get("question", ""))],
        "catalog_prompt": "Mocked catalog for evaluation.",
    }
    # Thread_id гарантирует изоляцию памяти LangGraph между потоками
    config = {"configurable": {"thread_id": f"eval_{case_id}", "client": client}}

    try:
        result = graph.invoke(initial_state, config=config)
        duration = time.time() - start_time
        passed = False
        issues = []
        outcome = expect.get("outcome")
        blocks = result.get("answer_blocks", [])
        text_answers = " ".join([b.markdown for b in blocks if getattr(b, "markdown", None)])
        used_tools = [r.tool for r in result.get("results", [])]
        raw_data = [getattr(r, "data", {}) for r in result.get("results", [])]
        raw_data_str = json.dumps(raw_data, default=str)

        if outcome == "answer":
            tools_any = expect.get("tools_any", [])
            if tools_any and not set(tools_any).intersection(set(used_tools)):
                issues.append(f"Missing tools {tools_any}. Used: {used_tools}")
            for fact in expect.get("facts", []):
                if not check_fact(fact, text_answers, raw_data_str):
                    issues.append(f"Fact '{fact['key']}' ({fact['value']}) missing.")
            if not issues:
                passed = True

        elif outcome == "clarify":
            understanding = result.get("understanding")
            if isinstance(understanding, dict):
                intent = understanding.get("intent")
            else:
                intent = getattr(understanding, "intent", "")
            if intent != expect.get("intent"):
                issues.append(f"Expected intent '{expect.get('intent')}', got '{intent}'")
            else:
                passed = True

        elif outcome in ["reject", "input_error"]:
            understanding = result.get("understanding")
            if isinstance(understanding, dict):
                intent = understanding.get("intent")
            else:
                intent = getattr(understanding, "intent", "")
            if intent not in ["invalid", "off_topic", "ambiguous"]:
                issues.append(f"Expected rejection intent, got '{intent}'")
            else:
                passed = True

        status = "passed" if passed else "failed"
        return {
            "type": status,
            "data": {
                "id": case_id,
                "status": status,
                "duration_seconds": round(duration, 2),
                "used_tools": used_tools,
                "issues": issues,
                "answer": text_answers,
            },
        }
    except Exception as e:
        return {
            "type": "failed",
            "data": {
                "id": case_id,
                "status": "failed",
                "duration_seconds": round(time.time() - start_time, 2),
                "issues": [f"Graph execution failed: {str(e)}"],
            },
        }


def run_eval(questions_path: Path, output_path: Path | None) -> dict:
    with open(questions_path, encoding="utf-8") as f:
        cases = yaml.load(f, Loader=UniqueKeyLoader)

    try:
        client = get_agent_client()
    except Exception as e:
        print(f"Warning: Could not init live client ({e}). Some tests will fail.")
        client = None

    graph = build_agent_graph()
    report = {"summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0}, "cases": []}

    print(f"Starting evaluation of {len(cases)} cases across 5 parallel threads...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_case, case, client, graph) for case in cases]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            report["summary"][res["type"]] += 1
            if res["type"] != "skipped":
                report["summary"]["total"] += 1
            report["cases"].append(res["data"])
            print(f"Completed case: {res['data']['id']} -> {res['data']['status']}")

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n--- Evaluation Summary ---")
    print(json.dumps(report["summary"], indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/eval-report.json")
    args = parser.parse_args()
    run_eval(args.questions, args.output)
