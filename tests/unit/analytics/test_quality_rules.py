"""Неоднозначные даты, смешанные валюты, проценты и разделитель в расчёте (B2-10)."""

from __future__ import annotations

import pytest

from djedai.analytics.quality import assess
from djedai.analytics.tools import run_sql
from djedai.contracts.catalog import DatasetCatalog


def legacy_catalog(catalog):
    missing = {"without_percent_sign", "ambiguous_values", "alternative_formats"}
    legacy = catalog.model_copy(
        update={
            "transformations": [
                entry.model_copy(
                    update={"detail": {k: v for k, v in entry.detail.items() if k not in missing}}
                )
                for entry in catalog.transformations
            ]
        }
    )
    return DatasetCatalog.model_validate_json(legacy.model_dump_json())


@pytest.fixture
def issues(quality_dataset):
    connection, catalog = quality_dataset
    allowed = {t.table_id for t in catalog.tables}

    def run(sql: str) -> dict[tuple[str, str], str]:
        result = run_sql(sql, connection=connection, allowed_tables=allowed)
        assert result.status == "ok", result.error
        report = assess(result, connection, catalog).quality
        return {(c.column, i.code): i.severity for c in report.columns for i in c.issues}

    return run


def test_date_without_day_month_order_blocks_grouping(issues):
    found = issues("SELECT d_ambiguous, COUNT(*) FROM edges GROUP BY d_ambiguous")
    assert found == {("d_ambiguous", "ambiguous_date"): "blocking"}


def test_date_without_day_month_order_only_shown_is_a_warning(issues):
    assert issues("SELECT d_ambiguous FROM edges") == {("d_ambiguous", "ambiguous_date"): "warning"}


def test_date_format_chosen_by_majority_warns(issues):
    assert issues("SELECT MIN(d_partial) FROM edges") == {
        ("d_partial", "ambiguous_date"): "warning"
    }


def test_mixed_currency_column_blocks_aggregation(issues):
    found = issues(
        "SELECT SUM(TRY_CAST(regexp_replace(price_mixed, '[^0-9]', '', 'g') AS INT)) FROM edges"
    )
    assert found == {("price_mixed", "mixed_currency"): "blocking"}


def test_mixed_currency_column_shown_as_text_is_fine(issues):
    assert issues("SELECT price_mixed FROM edges") == {}


def test_money_in_different_currencies_in_one_sum_blocks(issues):
    found = issues("SELECT SUM(rub + usd) FROM edges")
    assert found == {("rub", "mixed_currency"): "blocking", ("usd", "mixed_currency"): "blocking"}


def test_money_in_different_currencies_in_separate_sums_is_fine(issues):
    assert issues("SELECT SUM(rub), SUM(usd) FROM edges") == {}


def test_percent_scale_and_sum_warn(issues):
    assert issues("SELECT SUM(share) FROM edges") == {
        ("share", "percent_scale"): "warning",
        ("share", "percent_sum"): "warning",
    }
    assert issues("SELECT AVG(share) FROM edges") == {("share", "percent_scale"): "warning"}


def test_single_separator_warns(issues):
    assert issues("SELECT SUM(sep) FROM edges") == {("sep", "ambiguous_separator"): "warning"}


@pytest.mark.parametrize(
    ("expression", "column_name", "code"),
    [("AVG(share)", "share", "percent_scale"), ("MIN(d_partial)", "d_partial", "ambiguous_date")],
)
def test_old_catalog_recovers_missing_ambiguities_from_raw(
    quality_dataset, expression, column_name, code
):
    connection, catalog = quality_dataset
    legacy = legacy_catalog(catalog)
    before = legacy.model_dump(mode="json")
    result = run_sql(
        f"SELECT {expression} FROM edges",
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
    )
    assert result.status == "ok", result.error
    fresh = assess(result, connection, catalog)
    restored = assess(result, connection, legacy)
    expected = next(c for c in fresh.quality.columns if c.column == column_name)
    actual = next(c for c in restored.quality.columns if c.column == column_name)
    assert actual == expected
    assert any(issue.code == code for issue in actual.issues)
    assert restored.data == result.data
    assert legacy.model_dump(mode="json") == before


@pytest.mark.parametrize(
    ("expression", "code"), [("AVG(share)", "percent_scale"), ("MIN(d_partial)", "ambiguous_date")]
)
def test_old_catalog_reports_boundary_when_raw_check_is_unavailable(
    quality_dataset, monkeypatch, expression, code
):
    connection, catalog = quality_dataset
    result = run_sql(
        f"SELECT {expression} FROM edges",
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
    )
    assert result.status == "ok", result.error

    def unavailable(*args):
        raise ValueError("legacy view unavailable")

    monkeypatch.setattr("djedai.analytics.quality._quality_relation", unavailable)
    assessed = assess(result, connection, legacy_catalog(catalog))
    assert assessed.quality.lineage == "partial"
    assert assessed.quality.unresolved
    assert any(
        issue.code == code and issue.severity == "warning" and "не проверены" in issue.message
        for column in assessed.quality.columns
        for issue in column.issues
    )
    assert any(fact.kind == "data_quality" for fact in assessed.facts)
    assert assessed.data == result.data


def test_blocking_issues_offer_answers_that_name_the_column(quality_dataset):
    connection, catalog = quality_dataset
    result = run_sql(
        "SELECT d_ambiguous, COUNT(*) FROM edges GROUP BY 1",
        connection=connection,
        allowed_tables={t.table_id for t in catalog.tables},
    )
    (issue,) = assess(result, connection, catalog).quality.columns[0].issues
    assert issue.options == [
        "edges.d_ambiguous: день/месяц/год (%d/%m/%Y)",
        "edges.d_ambiguous: месяц/день/год (%m/%d/%Y)",
    ]
