"""CLI прогона HK_Datasets на маленьком CSV в формате организаторов: эталон и платформа сходятся."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE = "08_Фоновые_задания.csv"
HEADER = (
    "Код запуска;Код цепочки запусков;Задание;Сервис;Окружение;Способ запуска;Номер попытки;"
    "Начало (МСК);Окончание (МСК);Статус;Записей к обработке;Обработано записей;Код ошибки;"
    "Описание ошибки;Узел выполнения"
)


def write_dataset(cache: Path) -> None:
    lines = [HEADER]
    for index in range(1, 61):
        month = 1 + index % 12
        failed = index % 7 == 0
        lines.append(
            f"ФЗ-ЗП-{index:07d};ФЗ-ЦК-{(index + 1) // 2:07d};Обновление статусов;Доставка;"
            f"{'production' if index % 3 else 'staging'};По расписанию;{1 + index % 2};"
            f"2024-{month:02d}-0{1 + index % 9} 00:00:{index % 60:02d};"
            f"2024-{month:02d}-0{1 + index % 9} 00:05:{index % 60:02d};"
            f"{'Ошибка' if failed else 'Успешно'};{index * 10};{index * 10 - failed};"
            f"{'SOURCE_UNAVAILABLE' if failed else ''};"
            f"{'Источник данных недоступен' if failed else ''};worker-{index % 4}"
        )
    cache.mkdir()
    (cache / FILE).write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.integration
def test_check_cli_matches_reference(tmp_path: Path):
    manifest = json.loads((ROOT / "eval/hk_datasets_manifest.json").read_text(encoding="utf-8"))
    manifest["tables"] = [table for table in manifest["tables"] if table["file"] == FILE]
    manifest["tables"][0]["rows"] = 60
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    write_dataset(tmp_path / "cache")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_hk_datasets.py"),
            "--manifest", str(tmp_path / "manifest.json"),
            "--cache", str(tmp_path / "cache"),
            "--stages", "reference,csv,queries",
            "--output", str(tmp_path / "run"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )  # fmt: skip

    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8"))
    assert report["reference"][FILE]["rows"] == 60
    assert report["reference"][FILE]["header_matches_readme"]
    checks = report["csv"]["checks"]["tables"][FILE]
    assert checks["rows_match_readme"] and checks["columns_match_readme"]
    assert checks["type_mismatches"] == []
    assert checks["key_found"]
    results = report["csv"]["queries"]["results"]
    compared = [item for item in results if item["match"] is not None]
    assert compared and all(item["match"] for item in compared), compared
    assert (tmp_path / "run/report.md").exists()

    again = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_hk_datasets.py"),
            "--compare", str(tmp_path / "run/report.json"), str(tmp_path / "run/report.json"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )  # fmt: skip
    assert again.returncode == 0 and "csv.seconds" in again.stdout
