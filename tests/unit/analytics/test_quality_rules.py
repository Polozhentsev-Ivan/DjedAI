"""Неоднозначные даты, смешанные валюты, проценты и разделитель в расчёте (B2-10)."""

from __future__ import annotations

import pytest

from djedai.analytics.quality import assess
from djedai.analytics.tools import run_sql


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
