import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path("tests/fixtures/synthetic")
EXPECTATIONS = Path("eval/synthetic_expectations.json")


def verify(data_dir=FIXTURES, expectations=EXPECTATIONS):
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "scripts/verify_synthetic_data.py",
            "--data-dir",
            data_dir,
            "--expectations",
            expectations,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def rewrite_csv(path, mutate):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        rows = list(reader)
    mutate(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_references_are_independently_calculated_from_exports():
    result = verify()
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)["metrics"]
    assert actual["lag_days"] == 5
    assert actual["duplicate_rows"] == 208
    assert actual["seasonality_peak_month"] == 12
    assert actual["seasonality_peak_rows"] == 861
    assert actual["revenue_peak_month"] == 4
    assert actual["revenue_peak_amount"] == "253427853.81"


def test_references_match_newly_generated_data(tmp_path):
    output = tmp_path / "generated"
    subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "scripts/generate_synthetic_data.py",
            "--output-dir",
            output,
        ],
        capture_output=True,
        check=True,
    )
    result = verify(output)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change",
    [
        "campaign",
        "activity_scale",
        "calendar",
        "duplicate_id",
        "money",
        "duplicate_store",
        "missing_signal",
    ],
)
def test_independent_check_detects_corrupted_patterns(tmp_path, change):
    output = tmp_path / "data"
    shutil.copytree(FIXTURES, output)
    if change == "campaign":

        def shift_activity(rows):
            for index, row in enumerate(rows):
                if row["activity"] == "1":
                    row["activity"] = "0"
                    rows[index + 1]["activity"] = "1"
                    break

        rewrite_csv(output / "campaigns.csv", shift_activity)
        expected_error = "lag_days: expected 5, got 4"
    elif change == "activity_scale":

        def scale_activity(rows):
            for row in rows:
                if row["activity"] == "1":
                    row["activity"] = "7"

        rewrite_csv(output / "campaigns.csv", scale_activity)
        expected_error = "Campaign activity must be 0 or 1"
    elif change == "calendar":
        rewrite_csv(
            output / "campaigns.csv",
            lambda rows: rows.append({"date": "01.01.2024", "activity": "0"}),
        )
        expected_error = "Campaign calendar does not match reference bounds"
    elif change == "duplicate_id":
        rewrite_csv(output / "sales.csv", lambda rows: rows[-1].update(sale_id="unique_copy"))
        expected_error = "duplicate_rows: expected 208, got 207"
    elif change == "money":
        rewrite_csv(
            output / "sales.csv", lambda rows: rows[0].update(total_amount="999 999 999,00 руб.")
        )
        expected_error = "anomalies_max_amount:"
    elif change == "duplicate_store":  # дубль в справочнике: JOIN размножает продажи
        rewrite_csv(output / "stores.csv", lambda rows: rows.append(dict(rows[0])))
        expected_error = "sales.store_id → stores.store_id: cardinality expected 'N:1', got 'N:M'"
    else:
        (output / "campaigns.csv").unlink()
        expected_error = "campaigns.csv"
    result = verify(output)
    assert result.returncode == 1
    assert expected_error in result.stderr


def test_checker_rejects_empty_reference_set(tmp_path):
    expectations = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
    expectations["metrics"] = {}
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(expectations), encoding="utf-8")
    result = verify(expectations=path)
    assert result.returncode == 1
    assert "Incomplete or unknown reference metrics" in result.stderr
