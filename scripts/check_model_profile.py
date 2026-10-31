"""Run the three organizer profile controls using the pinned golden dataset (#225)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from djedai.agent.config import env_model_values, reasoning_profile  # noqa: E402
from djedai.config import get_limits  # noqa: E402
from scripts.eval_runner import DEFAULT_QUESTIONS, UniqueKeyLoader, run_eval  # noqa: E402

CONTROL_IDS = ("s1_overview_01", "s2_status_01", "s3_monthly_counts_01")


def check_controls(report: dict[str, Any]) -> dict[str, Any]:
    """A fast-path answer or partial/blocked report cannot confirm model readiness."""
    cases = {case["id"]: case for case in report["cases"]}
    checks = []
    for case_id in CONTROL_IDS:
        case = cases.get(case_id, {})
        events = case.get("diagnostics", [])
        verified = any(
            event.get("event") == "model_call_finished"
            and event.get("stage") == "verify"
            and event.get("status") == "success"
            for event in events
        )
        facts = case.get("fact_checks", [])
        passed = (
            report.get("status") == "completed"
            and case.get("status") in {"passed", "needs_review"}
            and case.get("automated_status") == "passed"
            and bool(facts)
            and all(fact.get("passed") for fact in facts)
            and verified
        )
        checks.append({"id": case_id, "passed": passed, "model_verify_completed": verified})
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-file", type=Path, help="Pinned golden CSV; see --download")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/model-profile/report.json")
    args = parser.parse_args(argv)

    questions = yaml.load(DEFAULT_QUESTIONS.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    by_id = {case["id"]: case for case in questions}
    # Keep questions and independent expected values in one source of truth.
    selected = [by_id[case_id] for case_id in CONTROL_IDS]
    with tempfile.TemporaryDirectory(prefix="djedai-model-profile-") as temporary:
        path = Path(temporary) / "questions.yaml"
        path.write_text(yaml.safe_dump(selected, allow_unicode=True), encoding="utf-8")
        report = run_eval(
            path, args.output, data_file=args.data_file, download=args.download, max_workers=1
        )

    values = env_model_values()
    endpoint = urlsplit(values.base_url or "")
    limits = get_limits()
    # Never serialize credentials, URL userinfo or query parameters.
    report["model_profile"] = {
        "model": values.name,
        "endpoint": urlunsplit(
            (endpoint.scheme, endpoint.netloc.split("@")[-1], endpoint.path, "", "")
        ),
        "reasoning": reasoning_profile(limits),
        "reasoning_supported": limits.model_reasoning_supported,
        "attempt_timeout_seconds": limits.model_timeout_seconds,
        "turn_timeout_seconds": limits.agent_turn_timeout_seconds,
        "max_retries": limits.model_max_retries,
        "max_output_tokens": limits.model_max_output_tokens,
        "context_tokens": limits.model_context_tokens,
        "scope": "direct_graph_numeric_controls_not_browser_or_semantic_acceptance",
        **check_controls(report),
    }
    try:
        report["model_profile"]["application_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        report["model_profile"]["application_sha"] = None
    temporary_output = args.output.with_name(args.output.name + ".tmp")
    temporary_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_output.replace(args.output)
    print(json.dumps(report["model_profile"], ensure_ascii=False, indent=2))
    return 0 if report["model_profile"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
