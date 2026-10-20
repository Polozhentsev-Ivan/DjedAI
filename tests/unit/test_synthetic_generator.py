import csv
import subprocess
import sys
from pathlib import Path


def test_generator_is_reproducible_and_matches_committed_fixtures(tmp_path):
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "scripts/generate_synthetic_data.py",
                "--output-dir",
                output,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert "Данные сгенерированы!" in result.stdout
    expected_names = {"stores.csv", "products.csv", "sales.csv", "campaigns.csv"}
    for output in outputs:
        assert {path.name for path in output.iterdir()} == expected_names
    for name in expected_names:
        generated = (outputs[0] / name).read_bytes()
        assert generated == (outputs[1] / name).read_bytes()
        assert generated == (Path("tests/fixtures/synthetic") / name).read_bytes()
    with (outputs[0] / "sales.csv").open(encoding="utf-8", newline="") as stream:
        sales = list(csv.DictReader(stream))
    unique_rows = {tuple(row.items()) for row in sales}
    assert len(sales) - len(unique_rows) == 208
    assert len({row["sale_id"] for row in sales}) == len(unique_rows)
