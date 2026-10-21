"""Validate golden v1 and independently check its references against the full CSV.

This checks the question set, not the agent. No application analytics or model
client is imported; numerical references come from csv/Counter/Decimal only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTIONS = ROOT / "eval/questions.yaml"
DEFAULT_MANIFEST = ROOT / "eval/datasets/hk_background_jobs.json"
TOOLS = {"describe_dataset", "describe_column", "value_counts", "run_sql"}
FACT_KEYS = {
    "dataset.table_count",
    "dataset.row_count",
    "dataset.column_count",
    "dataset.columns",
    "dataset.relation_count",
    "start.min",
    "start.max",
    "run.distinct",
    "chain.distinct",
    "status.counts",
    "env.counts",
    "trigger.counts",
    "job.distinct",
    "errors.null_count",
    "errors.non_null_count",
    "errors.distinct",
    "errors.top3",
    "processed.min",
    "processed.max",
    "processed.mean",
    "processed.median",
    "processed.null_count",
    "production.row_count",
    "production.error_count",
    "production.error_percent",
}
MSK = timezone(timedelta(hours=3))


class GoldenError(ValueError):
    """An invalid suite, unverified source, or failed reference check."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently losing expectations."""


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise GoldenError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldenError(message)


def _strings(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len({item.strip() for item in value}) == len(value)
    )


def load_suite(
    questions_path: Path = DEFAULT_QUESTIONS, manifest_path: Path = DEFAULT_MANIFEST
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read versioned questions and reject incomplete or ambiguous scoring records."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(isinstance(manifest, dict), "Manifest must be an object")
    require(manifest.get("schema_version") == 1, "Unsupported manifest schema_version")
    source, dialect = manifest["source"], manifest["csv"]
    require(
        source["repository_url"] == "https://gitverse.ru/kban/HK_Datasets",
        "Unexpected source repository",
    )
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source["revision"])), "Unpinned revision")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", source["sha256"])), "Invalid SHA-256")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source["git_blob_sha1"])), "Invalid blob SHA-1")
    require(type(source["size_bytes"]) is int and source["size_bytes"] > 0, "Invalid size")
    require(Path(source["file_name"]).name == source["file_name"], "Invalid source file name")
    expected_url = (
        "https://gitverse.ru/api/repos/kban/HK_Datasets/raw/commit/"
        f"{source['revision']}/{quote(source['file_name'])}"
    )
    require(source["download_url"] == expected_url, "Download URL must pin the source commit")
    require(
        dialect["encoding"] == "utf-8-sig" and dialect["delimiter"] == ";", "Invalid CSV format"
    )
    require(_strings(dialect["columns"], nonempty=True), "Invalid or duplicate columns")
    binding = manifest["binding"]["columns"]
    require(set(binding) == set(dialect["columns"]), "Incomplete SQL column mapping")
    require(len(set(binding.values())) == len(binding), "Duplicate SQL column mapping")

    cases = yaml.load(questions_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    require(isinstance(cases, list) and bool(cases), "Questions must be a nonempty YAML list")
    ids, normal_counts, edge_intents = set(), Counter(), set()
    for case in cases:
        require(isinstance(case, dict), "Each question must be a mapping")
        case_id = case.get("id")
        require(
            isinstance(case_id, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*", case_id) is not None
            and case_id not in ids,
            "Missing or duplicate question id, or invalid spelling",
        )
        ids.add(case_id)
        require(case.get("dataset") == manifest["dataset_id"], f"{case_id}: unknown dataset")
        scenario = case.get("scenario")
        require(
            type(scenario) is int and 1 <= scenario <= 10 or scenario == "edge",
            f"{case_id}: scenario",
        )
        require(
            isinstance(case.get("question"), str) and bool(case["question"].strip()),
            f"{case_id}: empty question",
        )
        expect = case["expect"]
        require(isinstance(expect, dict), f"{case_id}: expect must be a mapping")
        require(_strings(expect.get("tools_any")), f"{case_id}: tools_any must be a list")
        require(set(expect["tools_any"]) <= TOOLS, f"{case_id}: unknown tool")
        require(
            _strings(expect.get("must_explain"), nonempty=True),
            f"{case_id}: missing semantic rubric",
        )
        require(_strings(expect.get("must_not_claim", [])), f"{case_id}: invalid negative rubric")
        if expect.get("outcome") == "answer":
            require(expect.get("population") in manifest["populations"], f"{case_id}: population")
            require(
                scenario != "edge" and bool(expect["tools_any"]), f"{case_id}: answer/tool mismatch"
            )
            normal_counts[scenario] += 1
            facts = expect.get("facts")
            require(isinstance(facts, list) and bool(facts), f"{case_id}: missing reference facts")
            keys = set()
            for fact in facts:
                key, comparison = fact["key"], fact["comparison"]
                require(
                    key in FACT_KEYS and key not in keys, f"{case_id}: unknown/duplicate fact {key}"
                )
                keys.add(key)
                require(
                    isinstance(fact.get("unit"), str) and bool(fact["unit"].strip()),
                    f"{case_id}: missing unit",
                )
                require(
                    comparison in {"exact", "exact_unordered", "approx"}, f"{case_id}: comparison"
                )
                json.dumps(fact["value"], allow_nan=False)
                if comparison == "approx":
                    for field in ("value", "abs_tolerance"):
                        require(
                            type(fact.get(field)) in (int, float) and math.isfinite(fact[field]),
                            f"{case_id}: invalid {field}",
                        )
                    require(fact["abs_tolerance"] >= 0, f"{case_id}: negative tolerance")
                else:
                    require("abs_tolerance" not in fact, f"{case_id}: tolerance on exact fact")
                if comparison == "exact_unordered":
                    require(
                        isinstance(fact["value"], list), f"{case_id}: unordered fact must be a list"
                    )
        else:
            require(
                expect.get("outcome") == "clarify" and scenario == "edge", f"{case_id}: outcome"
            )
            require(expect.get("intent") in {"ambiguous", "invalid"}, f"{case_id}: clarify intent")
            edge_intents.add(expect["intent"])
            require(not expect.get("facts"), f"{case_id}: clarification must not invent a result")
            require(
                "clarification" in expect["blocks_include"],
                f"{case_id}: missing clarification block",
            )
            clarification = expect["clarification"]
            require(
                bool(clarification.get("resolves")), f"{case_id}: missing clarification purpose"
            )
            require(
                _strings(clarification.get("options"), nonempty=True)
                and len(clarification["options"]) >= 2,
                f"{case_id}: clarification options",
            )
            forbidden = expect.get("tools_forbidden", [])
            require(_strings(forbidden) and set(forbidden) <= TOOLS, f"{case_id}: forbidden tools")
            require(
                not set(forbidden) & set(expect["tools_any"]), f"{case_id}: contradictory tools"
            )
    require(
        normal_counts[1] >= 5 and normal_counts[2] >= 5,
        "Need at least 5 normal questions per scenario 1/2",
    )
    require(edge_intents == {"ambiguous", "invalid"}, "Need ambiguous and invalid examples")
    return cases, manifest


def file_identity(path: Path) -> dict[str, Any]:
    """Hash the full byte stream, including the UTF-8 BOM and CSV newlines."""
    size = path.stat().st_size
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha256.update(chunk)
            blob.update(chunk)
    return {"size_bytes": size, "sha256": sha256.hexdigest(), "git_blob_sha1": blob.hexdigest()}


def check_identity(path: Path, source: dict[str, Any]) -> dict[str, Any]:
    identity = file_identity(path)
    require(
        all(identity[key] == source[key] for key in identity),
        "CSV size/hash differs from the pinned full source",
    )
    return identity


def download_file(path: Path, source: dict[str, Any]) -> None:
    """Fetch a missing pinned file; never replace an existing cache file."""
    if path.exists():
        check_identity(path, source)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as temporary:
        part = Path(temporary.name)
        try:
            with urlopen(source["download_url"], timeout=60) as response:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= source["size_bytes"], "Download exceeds pinned file size")
                    temporary.write(chunk)
            temporary.close()
            check_identity(part, source)
            require(
                not path.exists(), "Cache file appeared during download; refusing to replace it"
            )
            part.rename(path)
        finally:
            temporary.close()
            part.unlink(missing_ok=True)


def compute_facts(path: Path, dialect: dict[str, Any]) -> dict[str, Any]:
    """Stream the original CSV without cleaning, deduplicating, or using SQL."""
    counts = {key: Counter() for key in ("Статус", "Окружение", "Способ запуска", "Код ошибки")}
    runs, chains, jobs = set(), set(), set()
    processed = Counter()
    total = processed_null = production = production_errors = 0
    start_min = start_max = None
    with path.open(encoding=dialect["encoding"], newline="") as stream:
        reader = csv.DictReader(stream, delimiter=dialect["delimiter"])
        require(reader.fieldnames == dialect["columns"], "CSV header does not match manifest")
        for row in reader:
            require(
                None not in row and None not in row.values(), f"Malformed CSV record {total + 1}"
            )
            total += 1
            for key, counter in counts.items():
                counter[row[key]] += 1
            if row["Код запуска"]:
                runs.add(row["Код запуска"])
            if row["Код цепочки запусков"]:
                chains.add(row["Код цепочки запусков"])
            if row["Задание"]:
                jobs.add(row["Задание"])
            if row["Обработано записей"]:
                processed[int(row["Обработано записей"])] += 1
            else:
                processed_null += 1
            if row["Начало (МСК)"]:
                start = datetime.strptime(row["Начало (МСК)"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=MSK
                )
                start_min = min(start_min, start) if start_min else start
                start_max = max(start_max, start) if start_max else start
            if row["Окружение"] == "production":
                production += 1
                production_errors += row["Статус"] == "Ошибка"
    require(total > 0, "CSV has no data rows")
    errors = counts["Код ошибки"]
    non_null_errors = {key: value for key, value in errors.items() if key != ""}
    processed_count = sum(processed.values())
    processed_sum = sum(value * count for value, count in processed.items())
    middle = []
    cumulative = 0
    for value, count in sorted(processed.items()):
        for position in ((processed_count - 1) // 2, processed_count // 2):
            if cumulative <= position < cumulative + count:
                middle.append(value)
        cumulative += count
    return {
        "dataset.table_count": 1,
        "dataset.relation_count": 0,
        "dataset.row_count": total,
        "dataset.column_count": len(dialect["columns"]),
        "dataset.columns": dialect["columns"],
        "start.min": start_min.isoformat() if start_min else None,
        "start.max": start_max.isoformat() if start_max else None,
        "run.distinct": len(runs),
        "chain.distinct": len(chains),
        "job.distinct": len(jobs),
        "status.counts": [list(pair) for pair in sorted(counts["Статус"].items())],
        "env.counts": [list(pair) for pair in sorted(counts["Окружение"].items())],
        "trigger.counts": [list(pair) for pair in sorted(counts["Способ запуска"].items())],
        "errors.null_count": errors[""],
        "errors.non_null_count": total - errors[""],
        "errors.distinct": len(non_null_errors),
        "errors.top3": [
            list(pair)
            for pair in sorted(non_null_errors.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
        ],
        "processed.null_count": processed_null,
        "processed.min": min(processed) if processed else None,
        "processed.max": max(processed) if processed else None,
        "processed.mean": Decimal(processed_sum) / processed_count if processed_count else None,
        "processed.median": sum(middle) / 2 if middle else None,
        "production.row_count": production,
        "production.error_count": production_errors,
        "production.error_percent": Decimal(100 * production_errors) / production
        if production
        else None,
    }


def fact_matches(fact: dict[str, Any], actual: Any) -> bool:
    """Compare reference data only; this does not score a model's answer."""
    expected = fact["value"]
    if fact["comparison"] == "approx":
        if actual is None or isinstance(actual, bool):
            return False
        return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal(
            str(fact["abs_tolerance"])
        )
    if fact["comparison"] == "exact_unordered":
        if not isinstance(actual, list):
            return False

        def canonical(value: Any) -> str:
            return json.dumps(value, sort_keys=True, ensure_ascii=False)

        return Counter(map(canonical, actual)) == Counter(map(canonical, expected))
    # JSON's numeric types may differ, but bool must never stand in for a count.
    return not isinstance(expected, bool) and not isinstance(actual, bool) and actual == expected


def verify_references(cases: list[dict[str, Any]], actual: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for case in cases:
        for fact in case["expect"].get("facts", []):
            checks.append(
                {
                    "question_id": case["id"],
                    "key": fact["key"],
                    "expected": fact["value"],
                    "actual": actual[fact["key"]],
                    "unit": fact["unit"],
                    "population": case["expect"]["population"],
                    "passed": fact_matches(fact, actual[fact["key"]]),
                }
            )
    return checks


def external_path(path: Path) -> Path:
    resolved = path.resolve()
    require(
        not resolved.is_relative_to(ROOT),
        "CSV caches and generated reports must be outside the repository",
    )
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--data-file", type=Path)
    parser.add_argument(
        "--download", action="store_true", help="Download the pinned full CSV if missing"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Check structure without checking CSV references",
    )
    parser.add_argument("--output", type=Path, help="Optional report outside the checkout")
    args = parser.parse_args(argv)
    report: dict[str, Any] = {
        "status": "error",
        "oracle_verified": False,
        "agent_evaluation": "not_run",
    }
    output = None
    try:
        output = external_path(args.output) if args.output else None
        cases, manifest = load_suite(args.questions, args.manifest)
        report.update(
            dataset=manifest["dataset_id"],
            questions=len(cases),
            scenarios=dict(Counter(str(case["scenario"]) for case in cases)),
        )
        if args.validate_only:
            require(
                not args.download and args.data_file is None,
                "--validate-only cannot also check/download a CSV",
            )
            report["status"] = "structure_valid"
        else:
            require(args.data_file is not None, "Provide --data-file or choose --validate-only")
            data_file = external_path(args.data_file)
            require(output != data_file, "Report must not overwrite the CSV")
            if args.download:
                download_file(data_file, manifest["source"])
            report["source_identity"] = check_identity(data_file, manifest["source"])
            checks = verify_references(cases, compute_facts(data_file, manifest["csv"]))
            report.update(checks=checks, oracle_verified=all(check["passed"] for check in checks))
            report["status"] = "verified" if report["oracle_verified"] else "reference_mismatch"
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as error:
        report["error"] = str(error)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] in {"structure_valid", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
