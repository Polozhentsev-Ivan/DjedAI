"""Golden contracts and independent hand-calculated checks; no model or full CSV."""

import copy
import csv
import hashlib
import importlib.util
import io
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from djedai.analytics.registry import TOOLS_REGISTRY
from djedai.contracts import ClarificationBlock
from djedai.data.csv_ingest import raw_table_name
from djedai.data.typed_views import normalize_column_names

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "verify_golden_references", ROOT / "scripts/verify_golden.py"
)
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


@pytest.fixture
def suite():
    return golden.load_suite()


def write_suite(tmp_path, cases, manifest):
    questions_path = tmp_path / "questions.yaml"
    manifest_path = tmp_path / "manifest.json"
    questions_path.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return questions_path, manifest_path


def tiny_rows():
    # Repeated chain, production cancellation, nonproduction error, zero and missing measure.
    records = [
        (
            "run1",
            "chain1",
            "A",
            "production",
            "По расписанию",
            1,
            "2025-01-01 10:00:00",
            "Ошибка",
            "0",
            "TIMEOUT",
        ),
        (
            "run2",
            "chain1",
            "A",
            "production",
            "По событию",
            2,
            "2025-01-02 11:00:00",
            "Успешно",
            "10",
            "",
        ),
        (
            "run3",
            "chain2",
            "B",
            "staging",
            "Вручную",
            1,
            "2025-01-03 12:00:00",
            "Ошибка",
            "30",
            "ACCESS_DENIED",
        ),
        (
            "run4",
            "chain3",
            "A",
            "production",
            "Вручную",
            1,
            "2025-01-04 13:00:00",
            "Отменено",
            "",
            "",
        ),
    ]
    return [
        {
            "Код запуска": run,
            "Код цепочки запусков": chain,
            "Задание": job,
            "Сервис": "Сервис",
            "Окружение": environment,
            "Способ запуска": trigger,
            "Номер попытки": attempt,
            "Начало (МСК)": start,
            "Окончание (МСК)": start,
            "Статус": status,
            "Записей к обработке": 42,
            "Обработано записей": processed,
            "Код ошибки": error,
            "Описание ошибки": "Описание" if error else "",
            "Узел выполнения": "worker-01",
        }
        for (
            run,
            chain,
            job,
            environment,
            trigger,
            attempt,
            start,
            status,
            processed,
            error,
        ) in records
    ]


def write_csv(path, dialect, rows):
    with path.open("w", encoding=dialect["encoding"], newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=dialect["columns"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def tiny_csv(tmp_path, suite):
    return write_csv(tmp_path / "tiny.csv", suite[1]["csv"], tiny_rows())


def source_identity(payload):
    # Independent byte-level identity formula; fixtures are a few hundred bytes.
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "git_blob_sha1": hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest(),
    }


def test_versioned_suite_has_usable_scenario_and_clarification_contracts(suite):
    cases, manifest = suite
    normal = Counter(case["scenario"] for case in cases if case["expect"]["outcome"] == "answer")
    assert normal[1] >= 5 and normal[2] >= 5
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expect"]["intent"] for case in cases if case["scenario"] == "edge"} == {
        "ambiguous",
        "invalid",
    }
    registry = {definition.name: definition for definition in TOOLS_REGISTRY}
    for case in cases:
        expect = case["expect"]
        assert set(expect["tools_any"]) <= registry.keys()
        assert set(expect.get("tools_forbidden", [])) <= registry.keys()
        assert expect["must_explain"]
        if expect["outcome"] == "clarify":
            block = ClarificationBlock(
                question=expect["clarification"]["resolves"],
                options=expect["clarification"]["options"],
            )
            assert len(block.options) >= 2
            assert not expect.get("facts")
            assert not set(expect.get("tools_forbidden", [])) & set(expect["tools_any"])
        else:
            assert expect["population"] in manifest["populations"]
            assert expect["facts"]
            assert all(fact["unit"].strip() for fact in expect["facts"])


def test_binding_covers_actual_normalized_columns_and_registry_parameters(suite):
    _, manifest = suite
    columns = manifest["csv"]["columns"]
    binding = manifest["binding"]
    assert len(columns) == 15
    assert binding["columns"] == dict(zip(columns, normalize_column_names(columns), strict=True))
    assert binding["raw_table_name"] == raw_table_name(manifest["source"]["file_name"], set())
    assert binding["table_name"] == binding["raw_table_name"].removeprefix("raw_")
    params = {
        "describe_dataset": {},
        "describe_column": {
            "table": binding["table_name"],
            "column": binding["columns"]["Обработано записей"],
        },
        "value_counts": {
            "table": binding["table_name"],
            "column": binding["columns"]["Статус"],
            "top_n": 3,
        },
        "run_sql": {"sql": f'SELECT COUNT(*) FROM "{binding["table_name"]}"'},
    }
    for definition in TOOLS_REGISTRY:
        if definition.name in params:
            definition.parameters_model.model_validate(params[definition.name])


def test_tiny_csv_counts_attempts_without_deduplicating_chains_and_uses_correct_denominator(
    tiny_csv, suite
):
    actual = golden.compute_facts(tiny_csv, suite[1]["csv"])
    assert actual["dataset.table_count"] == 1
    assert actual["dataset.relation_count"] == 0
    assert actual["dataset.row_count"] == 4
    assert actual["dataset.column_count"] == 15
    assert actual["dataset.columns"] == suite[1]["csv"]["columns"]
    assert actual["start.min"] == "2025-01-01T10:00:00+03:00"
    assert actual["start.max"] == "2025-01-04T13:00:00+03:00"
    assert actual["run.distinct"] == 4
    assert actual["chain.distinct"] == 3
    assert actual["job.distinct"] == 2
    assert dict(actual["status.counts"]) == {"Ошибка": 2, "Успешно": 1, "Отменено": 1}
    assert dict(actual["env.counts"]) == {"production": 3, "staging": 1}
    assert dict(actual["trigger.counts"]) == {"По расписанию": 1, "По событию": 1, "Вручную": 2}
    assert actual["errors.null_count"] == 2
    assert actual["errors.non_null_count"] == 2
    assert actual["errors.distinct"] == 2
    assert actual["errors.top3"] == [["ACCESS_DENIED", 1], ["TIMEOUT", 1]]
    assert actual["processed.null_count"] == 1
    assert actual["processed.min"] == 0
    assert actual["processed.max"] == 30
    assert actual["processed.mean"] == Decimal(40) / 3
    assert actual["processed.median"] == 10
    assert actual["production.row_count"] == 3
    assert actual["production.error_count"] == 1
    assert actual["production.error_percent"] == Decimal(100) / 3


@pytest.mark.parametrize("measure_values, median", [(["0", "10"], 5), (["", ""], None)])
def test_even_median_and_all_missing_measure_have_no_zero_imputation(
    tmp_path, suite, measure_values, median
):
    rows = tiny_rows()[:2]
    for row, value in zip(rows, measure_values, strict=True):
        row["Обработано записей"] = value
    path = write_csv(tmp_path / "measure.csv", suite[1]["csv"], rows)
    actual = golden.compute_facts(path, suite[1]["csv"])
    assert actual["processed.median"] == median
    if median is None:
        assert actual["processed.min"] is None
        assert actual["processed.max"] is None
        assert actual["processed.mean"] is None
        assert actual["processed.null_count"] == 2


def test_approximate_values_enforce_boundary_and_percent_units():
    fact = {"comparison": "approx", "value": 10, "abs_tolerance": 0.005}
    assert golden.fact_matches(fact, Decimal("10.005"))
    assert golden.fact_matches(fact, Decimal("9.995"))
    assert not golden.fact_matches(fact, Decimal("10.0050001"))
    assert not golden.fact_matches(fact, Decimal("9.9949999"))
    assert not golden.fact_matches(fact, None)
    percent = {"comparison": "approx", "value": 6.346694925440501, "abs_tolerance": 0.005}
    assert golden.fact_matches(percent, Decimal("6.35"))
    assert not golden.fact_matches(percent, Decimal("0.06346694925440501"))


def test_exact_unordered_preserves_duplicate_multiplicity():
    fact = {"comparison": "exact_unordered", "value": [["A", 2], ["B", 1], ["A", 2]]}
    assert golden.fact_matches(fact, [["B", 1], ["A", 2], ["A", 2]])
    assert not golden.fact_matches(fact, [["A", 2], ["B", 1]])
    assert not golden.fact_matches(fact, [["A", 2], ["B", 1], ["B", 1]])
    assert not golden.fact_matches({"comparison": "exact", "value": 1}, True)


def test_reference_report_retains_unit_population_and_detects_wrong_value():
    cases = [
        {
            "id": "production",
            "expect": {
                "population": "production_attempt_rows",
                "facts": [
                    {
                        "key": "production.error_percent",
                        "comparison": "approx",
                        "value": 50,
                        "abs_tolerance": 0.005,
                        "unit": "процент от всех попыток production",
                    }
                ],
            },
        }
    ]
    checks = golden.verify_references(cases, {"production.error_percent": Decimal("0.5")})
    assert checks[0]["passed"] is False
    assert checks[0]["unit"] == "процент от всех попыток production"
    assert checks[0]["population"] == "production_attempt_rows"


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_id",
        "unknown_fact",
        "negative_tolerance",
        "exact_tolerance",
        "too_few",
        "one_option",
        "trimmed_duplicate_options",
        "column_mapping",
        "unknown_tool",
    ],
)
def test_schema_rejects_ambiguous_or_incomplete_scoring_records(tmp_path, suite, change):
    cases, manifest = copy.deepcopy(suite)
    if change == "duplicate_id":
        cases[1]["id"] = cases[0]["id"]
    elif change == "unknown_fact":
        cases[0]["expect"]["facts"][0]["key"] = "revenue.total"
    elif change == "negative_tolerance":
        cases[0]["expect"]["facts"][0].update(comparison="approx", abs_tolerance=-0.01)
    elif change == "exact_tolerance":
        cases[0]["expect"]["facts"][0]["abs_tolerance"] = 0.01
    elif change == "too_few":
        cases = [case for case in cases if case["scenario"] != 1] + [
            case for case in cases if case["scenario"] == 1
        ][:4]
    elif change in {"one_option", "trimmed_duplicate_options"}:
        edge = next(case for case in cases if case["scenario"] == "edge")
        edge["expect"]["clarification"]["options"] = (
            ["Выбор"] if change == "one_option" else ["Выбор", " Выбор "]
        )
    elif change == "column_mapping":
        del manifest["binding"]["columns"]["Код ошибки"]
    else:
        cases[0]["expect"]["tools_any"] = ["delete_all"]
    questions_path, manifest_path = write_suite(tmp_path, cases, manifest)
    with pytest.raises(golden.GoldenError):
        golden.load_suite(questions_path, manifest_path)


def test_duplicate_yaml_key_is_not_silently_overwritten(tmp_path, suite):
    questions_path, manifest_path = write_suite(tmp_path, *copy.deepcopy(suite))
    questions_path.write_text("- id: original\n  id: overwritten\n", encoding="utf-8")
    with pytest.raises(golden.GoldenError, match="Duplicate"):
        golden.load_suite(questions_path, manifest_path)


@pytest.mark.parametrize("change", ["header", "extra_cell", "missing_cell"])
def test_csv_rejects_wrong_header_or_malformed_records(tiny_csv, suite, change):
    content = tiny_csv.read_text(encoding="utf-8-sig")
    if change == "header":
        content = content.replace("Код запуска;", "Другой заголовок;", 1)
    else:
        lines = content.splitlines()
        lines[1] = (
            lines[1] + ";unexpected" if change == "extra_cell" else lines[1].rsplit(";", 1)[0]
        )
        content = "\n".join(lines) + "\n"
    tiny_csv.write_text(content, encoding="utf-8-sig")
    with pytest.raises(golden.GoldenError):
        golden.compute_facts(tiny_csv, suite[1]["csv"])


def test_full_byte_identity_includes_bom_and_rejects_same_size_corruption(tiny_csv):
    payload = tiny_csv.read_bytes()
    expected = source_identity(payload)
    assert payload.startswith(b"\xef\xbb\xbf")
    assert golden.file_identity(tiny_csv) == expected
    assert golden.check_identity(tiny_csv, expected) == expected
    tiny_csv.write_bytes(payload.replace(b"run1", b"fake", 1))
    with pytest.raises(golden.GoldenError, match="size/hash"):
        golden.check_identity(tiny_csv, expected)


def test_validate_only_is_explicitly_not_oracle_or_agent_success(capsys):
    assert golden.main(["--validate-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "structure_valid"
    assert report["oracle_verified"] is False
    assert report["agent_evaluation"] == "not_run"
    assert "checks" not in report
    assert golden.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "error"
    assert report["oracle_verified"] is False


def test_valid_source_identity_cannot_hide_wrong_reference_values(
    tmp_path, tiny_csv, suite, capsys
):
    cases, manifest = copy.deepcopy(suite)
    manifest["source"].update(source_identity(tiny_csv.read_bytes()))
    questions_path, manifest_path = write_suite(tmp_path, cases, manifest)
    assert (
        golden.main(
            [
                "--questions",
                str(questions_path),
                "--manifest",
                str(manifest_path),
                "--data-file",
                str(tiny_csv),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "reference_mismatch"
    assert report["oracle_verified"] is False
    assert report["agent_evaluation"] == "not_run"
    assert any(check["passed"] is False for check in report["checks"])


def test_pinned_download_verifies_bytes_and_never_replaces_cache(tmp_path, monkeypatch):
    payload = b"\xef\xbb\xbfheader;value\r\nrow;42\r\n"
    source = {**source_identity(payload), "download_url": "https://example.invalid/pinned.csv"}
    requests = []

    def open_url(url, *, timeout):
        requests.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(golden, "urlopen", open_url)
    target = tmp_path / "cache" / "data.csv"
    golden.download_file(target, source)
    assert target.read_bytes() == payload
    assert len(requests) == 1
    golden.download_file(target, source)
    assert len(requests) == 1
    target.write_bytes(b"existing cache must survive")
    with pytest.raises(golden.GoldenError):
        golden.download_file(target, source)
    assert target.read_bytes() == b"existing cache must survive"
    assert len(requests) == 1
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("response", [b"too-long-payload", b"bad", b"evil"])
def test_failed_download_cleans_partial_file_without_publishing_cache(
    tmp_path, monkeypatch, response
):
    source = {**source_identity(b"good"), "download_url": "https://example.invalid/pinned.csv"}
    monkeypatch.setattr(golden, "urlopen", lambda *args, **kwargs: io.BytesIO(response))
    target = tmp_path / "cache" / "data.csv"
    with pytest.raises(golden.GoldenError):
        golden.download_file(target, source)
    assert not target.exists()
    assert not list(target.parent.iterdir())
