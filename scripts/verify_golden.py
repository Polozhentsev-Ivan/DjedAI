"""Validate the complete golden suite and independently check its CSV references.

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
import sys
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
SCENARIO_FACT_KEYS = {
    "monthly.counts",
    "quarterly.counts",
    "quarterly.errors",
    "env_status.counts",
    "env_trigger.counts",
    "service.top5",
    "duplicates.full_rows",
    "chains.repeated",
    "duration.min_seconds",
    "duration.max_seconds",
    "duration.mean_seconds",
    "duration.over_3600_count",
    "month_of_year.peak_month",
    "month_of_year.peak_daily_mean",
    "weekday.counts",
    "weekday.peak_day",
    "weekday.peak_daily_mean",
    "july.counts",
    "july.change_percent",
    "correlation.planned_processed",
    "correlation.daily_attempts_errors",
    "lag.best_days",
    "lag.best_r",
    "lag.plus5_r",
    "forecast.base7",
    "forecast.base7_total",
    "forecast.growth7",
    "forecast.growth7_total",
    "forecast.holdout7",
    "forecast.holdout_mae",
}
FACT_KEYS |= SCENARIO_FACT_KEYS
CHART_TYPES = {
    "line",
    "bar",
    "area",
    "scatter",
    "pie",
    "histogram",
    "heatmap",
    "box",
    "table",
    "kpi",
}
REQUIREMENTS = {
    "charts",
    "dashboards",
    "memory",
    "seasonality",
    "segments",
    "correlation",
    "forecast",
}
PLANNED_TOOLS = {
    "time_aggregate",
    "find_duplicates",
    "detect_anomalies",
    "seasonality",
    "segment_profile",
    "compare_segments",
    "correlation",
    "lagged_correlation",
    "forecast",
}


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
            isinstance(case.get("question"), str)
            and (
                bool(case["question"].strip())
                or case.get("expect", {}).get("outcome") == "input_error"
            ),
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
                scenario != "edge"
                and (bool(expect["tools_any"]) or bool(expect.get("reuse_result_from"))),
                f"{case_id}: answer/tool mismatch",
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
        elif expect.get("outcome") == "clarify":
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
        else:
            require(scenario == "edge", f"{case_id}: rejection must be edge")
            require(expect.get("outcome") in {"reject", "input_error"}, f"{case_id}: outcome")
            require(
                not expect.get("facts") and not expect["tools_any"], f"{case_id}: invented result"
            )
            require(set(expect.get("tools_forbidden", [])) == TOOLS, f"{case_id}: forbid analytics")
            if expect["outcome"] == "input_error":
                require(not case["question"].strip(), f"{case_id}: input must be empty")
                require(
                    expect.get("input_policy")
                    == {"ui_submit": False, "api_status": 422, "model_called": False},
                    f"{case_id}: input validation policy",
                )
            else:
                require(
                    expect.get("intent") in {"invalid", "off_topic"}, f"{case_id}: reject intent"
                )
                require(expect.get("blocks_include") == ["text"], f"{case_id}: rejection text")
    for scenario in range(1, 11):
        require(
            normal_counts[scenario] >= (5 if scenario <= 6 else 3),
            f"Need more questions for scenario {scenario}",
        )
    require(sum(case["scenario"] == "edge" for case in cases) >= 10, "Need at least 10 edge cases")
    require(edge_intents == {"ambiguous", "invalid"}, "Need ambiguous and invalid examples")
    validate_scenario_contracts(cases, manifest)
    return cases, manifest


def validate_scenario_contracts(cases: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Validate evaluation rubrics, without pretending to execute their features."""
    planned = manifest.get("planned_tools", {})
    require(
        manifest.get("scenario_source", {}).get("covered") == list(range(1, 11)),
        "Scenario source must cover all ten scenarios",
    )
    require(isinstance(planned, dict) and set(planned) == PLANNED_TOOLS, "Planned tool catalog")
    require(
        all(type(issue) is int and issue > 0 for issue in planned.values()), "Planned tool issues"
    )
    previous = {}
    for case in cases:
        case_id, expect = case["id"], case["expect"]
        require(
            set(case) <= {"id", "scenario", "dataset", "question", "context", "expect"},
            f"{case_id}: unknown question field",
        )
        future = expect.get("tools_planned", [])
        require(
            _strings(future) and set(future) <= set(planned), f"{case_id}: unknown planned tool"
        )
        require(not set(future) & TOOLS, f"{case_id}: registered tool marked planned")
        capabilities = expect.get("requirements", [])
        require(
            _strings(capabilities) and set(capabilities) <= REQUIREMENTS, f"{case_id}: requirements"
        )
        context = case.get("context")
        dependencies = []
        if context is not None:
            require(isinstance(context, dict), f"{case_id}: context must be a mapping")
            require(
                set(context) == {"previous_cases", "same_chat"},
                f"{case_id}: context may contain only real question references",
            )
            dependencies = context["previous_cases"]
            require(_strings(dependencies, nonempty=True), f"{case_id}: context questions")
            require(context["same_chat"] is True, f"{case_id}: same chat required")
            require(
                all(
                    dep in previous and previous[dep]["dataset"] == case["dataset"]
                    for dep in dependencies
                ),
                f"{case_id}: context must reference earlier cases in same dataset",
            )
            require("memory" in capabilities, f"{case_id}: context requires memory")
        require(case["scenario"] != 6 or bool(context), f"{case_id}: scenario 6 requires context")
        if "reuse_result_from" in expect:
            require(
                expect["reuse_result_from"] in dependencies,
                f"{case_id}: reuse requires prior context",
            )
        if "reuse_dashboard_from" in expect:
            require(
                expect["reuse_dashboard_from"] in dependencies,
                f"{case_id}: dashboard reuse requires prior context",
            )
            require(
                expect.get("dashboard") is not None,
                f"{case_id}: dashboard reuse requires a dashboard expectation",
            )
        facts = {fact["key"]: fact for fact in expect.get("facts", [])}
        chart = expect.get("chart")
        if chart is not None:
            require(isinstance(chart, dict), f"{case_id}: chart mapping")
            require(
                _strings(chart.get("types_any"), nonempty=True)
                and set(chart["types_any"]) <= CHART_TYPES,
                f"{case_id}: chart types",
            )
            require(
                chart.get("x_kind") in {"time", "category", "number"}, f"{case_id}: chart axis kind"
            )
            for field in ("x_meaning", "y_unit"):
                require(
                    isinstance(chart.get(field), str) and bool(chart[field].strip()),
                    f"{case_id}: chart {field}",
                )
            require(
                chart.get("data_fact") in facts, f"{case_id}: chart must refer to a checked fact"
            )
            require(
                type(chart.get("min_points")) is int and chart["min_points"] > 0,
                f"{case_id}: chart points",
            )
            value = facts[chart["data_fact"]]["value"]
            require(
                isinstance(value, list)
                or chart["min_points"] == 1
                and set(chart["types_any"]) <= {"kpi"},
                f"{case_id}: scalar chart fact requires a single KPI",
            )
            require(
                not isinstance(value, list) or chart["min_points"] == len(value),
                f"{case_id}: chart must retain all reference points",
            )
            require(chart.get("result_backed") is True, f"{case_id}: chart result provenance")
            require("charts" in capabilities, f"{case_id}: chart capability")
        require(case["scenario"] != 4 or chart is not None, f"{case_id}: scenario 4 requires chart")
        dashboard = expect.get("dashboard")
        if dashboard is not None:
            require(isinstance(dashboard, dict), f"{case_id}: dashboard mapping")
            require(dashboard.get("persisted") is True, f"{case_id}: dashboard persistence")
            require(
                dashboard.get("refresh") == "recompute_recipes",
                f"{case_id}: dashboard refresh recipe",
            )
            widgets = dashboard.get("widgets")
            require(isinstance(widgets, list) and bool(widgets), f"{case_id}: dashboard widgets")
            widget_ids = set()
            for widget in widgets:
                require(isinstance(widget, dict), f"{case_id}: widget mapping")
                widget_id = widget.get("id")
                require(
                    isinstance(widget_id, str)
                    and bool(widget_id.strip())
                    and widget_id not in widget_ids,
                    f"{case_id}: widget id",
                )
                widget_ids.add(widget_id)
                require(
                    _strings(widget.get("facts"), nonempty=True)
                    and set(widget["facts"]) <= set(facts),
                    f"{case_id}: widget reference facts",
                )
                require(
                    _strings(widget.get("chart_types_any"), nonempty=True)
                    and set(widget["chart_types_any"]) <= CHART_TYPES,
                    f"{case_id}: widget chart types",
                )
            require("dashboards" in capabilities, f"{case_id}: dashboard capability")
        require(
            case["scenario"] != 5 or dashboard is not None,
            f"{case_id}: scenario 5 requires dashboard",
        )
        previous[case_id] = case


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


def pearson(pairs: list[tuple[int, int]]) -> float | None:
    """Pearson from integer sufficient statistics; constant inputs are undefined."""
    n = len(pairs)
    sx, sy = sum(x for x, _ in pairs), sum(y for _, y in pairs)
    xx = n * sum(x * x for x, _ in pairs) - sx * sx
    yy = n * sum(y * y for _, y in pairs) - sy * sy
    return (
        (n * sum(x * y for x, y in pairs) - sx * sy) / math.sqrt(xx * yy)
        if xx > 0 and yy > 0
        else None
    )


def compute_scenario_facts(path: Path, dialect: dict[str, Any]) -> dict[str, Any]:
    """Independent reference math for scenarios 3–10; no production tools or SQL."""
    monthly, quarterly, quarter_errors = Counter(), Counter(), Counter()
    env_status, env_trigger, services, chains = Counter(), Counter(), Counter(), Counter()
    daily, daily_errors = Counter(), Counter()
    weekday_counts, calendar_month_counts = Counter(), Counter()
    runs, repeated_runs = set(), set()
    pairs, durations = [], []
    with path.open(encoding=dialect["encoding"], newline="") as stream:
        reader = csv.DictReader(stream, delimiter=dialect["delimiter"])
        require(reader.fieldnames == dialect["columns"], "CSV header does not match manifest")
        for index, row in enumerate(reader, 1):
            require(None not in row and None not in row.values(), f"Malformed CSV record {index}")
            run = row["Код запуска"]
            if run in runs:
                repeated_runs.add(run)
            runs.add(run)
            chains[row["Код цепочки запусков"]] += 1
            env_status[row["Окружение"], row["Статус"]] += 1
            env_trigger[row["Окружение"], row["Способ запуска"]] += 1
            services[row["Сервис"]] += 1
            if row["Записей к обработке"] and row["Обработано записей"]:
                pairs.append((int(row["Записей к обработке"]), int(row["Обработано записей"])))
            if not row["Начало (МСК)"]:
                continue
            start = datetime.strptime(row["Начало (МСК)"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
            month, quarter = start.strftime("%Y-%m"), f"{start.year}-Q{(start.month - 1) // 3 + 1}"
            monthly[month] += 1
            quarterly[quarter] += 1
            quarter_errors[quarter] += row["Статус"] == "Ошибка"
            daily[start.date()] += 1
            daily_errors[start.date()] += row["Статус"] == "Ошибка"
            weekday_counts[start.weekday()] += 1
            calendar_month_counts[start.month] += 1
            if row["Окончание (МСК)"]:
                end = datetime.strptime(row["Окончание (МСК)"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=MSK
                )
                require(end >= start, "Negative attempt duration")
                durations.append(int((end - start).total_seconds()))
    # A repeated full row necessarily repeats its run ID. Only that subset needs
    # full tuple comparisons; unique run IDs prove zero duplicates without a huge set.
    duplicates = Counter()
    if repeated_runs:
        with path.open(encoding=dialect["encoding"], newline="") as stream:
            for row in csv.DictReader(stream, delimiter=dialect["delimiter"]):
                if row["Код запуска"] in repeated_runs:
                    duplicates[tuple(row[column] for column in dialect["columns"])] += 1
    days = []
    if daily:
        days = [min(daily) + timedelta(days=i) for i in range((max(daily) - min(daily)).days + 1)]
    month_days, weekday_days = (
        Counter(day.month for day in days),
        Counter(day.weekday() for day in days),
    )
    month_rates = {m: Decimal(calendar_month_counts[m]) / count for m, count in month_days.items()}
    weekday_rates = {d: Decimal(weekday_counts[d]) / count for d, count in weekday_days.items()}
    peak_month = min(month_rates, key=lambda m: (-month_rates[m], m)) if month_rates else None
    peak_day = min(weekday_rates, key=lambda d: (-weekday_rates[d], d)) if weekday_rates else None
    lag_values = {}
    for lag in range(-7, 8):
        lag_values[lag] = pearson(
            [
                (daily[day], daily_errors[days[i + lag]])
                for i, day in enumerate(days)
                if 0 <= i + lag < len(days)
            ]
        )
    usable_lags = {lag: r for lag, r in lag_values.items() if r is not None}
    best_lag = (
        min(usable_lags, key=lambda lag: (-abs(usable_lags[lag]), abs(lag), lag))
        if usable_lags
        else None
    )
    base, growth, holdout = [], [], []
    if len(days) >= 7:
        for offset, day in enumerate(days[-7:], 1):
            future = (days[-1] + timedelta(days=offset)).isoformat()
            base.append([future, daily[day]])
            growth.append([future, float(Decimal(daily[day]) * Decimal("1.1"))])
    if len(days) >= 14:
        holdout = [
            [day.isoformat(), daily[days[-14 + i]], daily[day]] for i, day in enumerate(days[-7:])
        ]
    july = [[month, count] for month, count in sorted(monthly.items()) if month.endswith("-07")]
    return {
        "monthly.counts": [list(pair) for pair in sorted(monthly.items())],
        "quarterly.counts": [list(pair) for pair in sorted(quarterly.items())],
        "quarterly.errors": [[quarter, quarter_errors[quarter]] for quarter in sorted(quarterly)],
        "env_status.counts": [
            [env, status, count] for (env, status), count in sorted(env_status.items())
        ],
        "env_trigger.counts": [
            [env, trigger, count] for (env, trigger), count in sorted(env_trigger.items())
        ],
        "service.top5": [
            list(pair)
            for pair in sorted(services.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        ],
        "duplicates.full_rows": sum(count - 1 for count in duplicates.values()),
        "chains.repeated": sum(count > 1 for chain, count in chains.items() if chain),
        "duration.min_seconds": min(durations) if durations else None,
        "duration.max_seconds": max(durations) if durations else None,
        "duration.mean_seconds": Decimal(sum(durations)) / len(durations) if durations else None,
        "duration.over_3600_count": sum(value > 3600 for value in durations),
        "month_of_year.peak_month": peak_month,
        "month_of_year.peak_daily_mean": month_rates.get(peak_month),
        "weekday.counts": [[day, weekday_counts[day]] for day in sorted(weekday_days)],
        "weekday.peak_day": peak_day,
        "weekday.peak_daily_mean": weekday_rates.get(peak_day),
        "july.counts": july,
        "july.change_percent": Decimal(100 * (july[-1][1] - july[-2][1])) / july[-2][1]
        if len(july) >= 2 and july[-2][1]
        else None,
        "correlation.planned_processed": pearson(pairs),
        "correlation.daily_attempts_errors": pearson(
            [(daily[day], daily_errors[day]) for day in days]
        ),
        "lag.best_days": best_lag,
        "lag.best_r": usable_lags.get(best_lag),
        "lag.plus5_r": lag_values[5],
        "forecast.base7": base,
        "forecast.base7_total": sum(row[1] for row in base),
        "forecast.growth7": growth,
        "forecast.growth7_total": sum(Decimal(str(row[1])) for row in growth),
        "forecast.holdout7": holdout,
        "forecast.holdout_mae": Decimal(sum(abs(row[1] - row[2]) for row in holdout)) / len(holdout)
        if holdout
        else None,
    }


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
        **compute_scenario_facts(path, dialect),
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

    # JSON's numeric types may differ, but nested bools cannot stand in for counts.
    def contains_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return True
        if isinstance(value, list):
            return any(contains_bool(item) for item in value)
        if isinstance(value, dict):
            return any(contains_bool(item) for item in value.values())
        return False

    return not contains_bool(expected) and not contains_bool(actual) and actual == expected


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
    # Keep UTF-8 reports readable, but emit valid ASCII-escaped JSON when an
    # older Windows console cannot represent a unit such as “окружение × статус”.
    try:
        rendered.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        rendered = json.dumps(report, ensure_ascii=True, indent=2, default=str) + "\n"
    print(rendered, end="")
    return 0 if report["status"] in {"structure_valid", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
