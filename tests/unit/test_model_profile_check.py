"""Model readiness needs verified golden numbers, not just HTTP success."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import check_model_profile as smoke  # noqa: E402


def completed_report():
    return {
        "status": "completed",
        "cases": [
            {
                "id": case_id,
                "status": "needs_review",
                "automated_status": "passed",
                "fact_checks": [{"passed": True}],
                "diagnostics": [
                    {"event": "model_call_finished", "stage": "verify", "status": "success"}
                ],
            }
            for case_id in smoke.CONTROL_IDS
        ],
    }


def test_controls_allow_pending_human_review_without_claiming_semantic_acceptance():
    report = completed_report()
    original = copy.deepcopy(report)
    assert smoke.check_controls(report)["passed"] is True
    assert report == original


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "blocked",
        "failed",
        "wrong_fact",
        "no_facts",
        "fast_path",
        "verify_error",
        "partial",
    ],
)
def test_incomplete_or_unverified_controls_fail(defect):
    report = completed_report()
    case = report["cases"][0]
    if defect == "missing":
        report["cases"].pop(0)
    elif defect in {"blocked", "failed"}:
        case["status"] = defect
    elif defect == "wrong_fact":
        case["fact_checks"][0]["passed"] = False
    elif defect == "no_facts":
        case["fact_checks"] = []
    elif defect == "fast_path":
        case["diagnostics"] = [{"event": "fast_path_selected"}]
    elif defect == "verify_error":
        case["diagnostics"][0]["status"] = "error"
    else:
        report["status"] = "running"
    assert smoke.check_controls(report)["passed"] is False


@pytest.mark.parametrize("passed", [False, True])
def test_cli_uses_golden_cases_serially_and_writes_safe_metadata(tmp_path, monkeypatch, passed):
    original = yaml.safe_load(smoke.DEFAULT_QUESTIONS.read_text(encoding="utf-8"))
    output = tmp_path / "report.json"
    data = tmp_path / "source.csv"

    def run(questions, output_path, **kwargs):
        selected = yaml.safe_load(questions.read_text(encoding="utf-8"))
        assert selected == [case for case in original if case["id"] in smoke.CONTROL_IDS]
        assert kwargs == {"data_file": data, "download": False, "max_workers": 1}
        assert output_path == output
        report = completed_report()
        if not passed:
            report["cases"][0]["automated_status"] = "failed"
        return report

    monkeypatch.setattr(smoke, "run_eval", run)
    monkeypatch.setattr(
        smoke,
        "env_model_values",
        lambda: SimpleNamespace(
            name="test-model", base_url="http://user:secret@localhost:8000/v1?key=hidden"
        ),
    )
    assert smoke.main(["--data-file", str(data), "--output", str(output)]) == (0 if passed else 1)
    content = output.read_text(encoding="utf-8")
    profile = json.loads(content)["model_profile"]
    assert profile["endpoint"] == "http://localhost:8000/v1"
    assert profile["passed"] is passed
    assert "secret" not in content and "hidden" not in content
    assert not output.with_suffix(".json.tmp").exists()
