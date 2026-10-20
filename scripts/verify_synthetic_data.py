"""Check exported CSVs against fixed references without application analytics."""

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

SALES_COLUMNS = (
    "sale_id",
    "date",
    "store_id",
    "product_id",
    "quantity",
    "discount_pct",
    "total_amount",
)
METRIC_NAMES = {
    "total_rows_in_sales",
    "duplicate_rows",
    "missing_quantity_rows",
    "seasonality_peak_month",
    "seasonality_peak_rows",
    "revenue_peak_month",
    "revenue_peak_amount",
    "lag_days",
    "lag_spike_date",
    "lag_spike_rows",
    "anomalies_max_amount",
}


def read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(columns):
            raise ValueError(f"{path.name}: unexpected columns")
        rows = list(reader)
    if not rows or any(None in row or None in row.values() for row in rows):
        raise ValueError(f"{path.name}: missing or incomplete rows")
    return rows


def parse_date(value: str):
    return datetime.strptime(value, "%d.%m.%Y").date()


def parse_money(value: str) -> Decimal:
    if not value.endswith(" руб."):
        raise ValueError(f"Expected RUB amount: {value!r}")
    amount = Decimal(value.removesuffix(" руб.").replace(" ", "").replace(",", "."))
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"Invalid amount: {value!r}")
    return amount


def correlation(xs: list[int], ys: list[int]) -> float:
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    xx = sum((x - mean_x) ** 2 for x in xs)
    yy = sum((y - mean_y) ** 2 for y in ys)
    if not xx or not yy:
        raise ValueError("Lag cannot be determined from a constant signal")
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / math.sqrt(
        xx * yy
    )


def calculate_metrics(
    data_dir: Path, lag_search_days: list[int], calendar_bounds: dict[str, str]
) -> dict[str, int | str]:
    sales = read_csv(data_dir / "sales.csv", SALES_COLUMNS)
    campaigns = read_csv(data_dir / "campaigns.csv", ("date", "activity"))
    stores = read_csv(data_dir / "stores.csv", ("store_id", "city", "region"))
    products = read_csv(
        data_dir / "products.csv", ("product_id", "product_name", "category", "price")
    )
    if not {row["store_id"] for row in sales} <= {row["store_id"] for row in stores}:
        raise ValueError("Unknown store in sales")
    if not {row["product_id"] for row in sales} <= {row["product_id"] for row in products}:
        raise ValueError("Unknown product in sales")

    monthly_rows = Counter()
    monthly_revenue = defaultdict(Decimal)
    daily_rows = Counter()
    amounts = []
    for row in sales:
        day = parse_date(row["date"])
        amount = parse_money(row["total_amount"])
        monthly_rows[day.month] += 1
        monthly_revenue[day.month] += amount
        daily_rows[day] += 1
        amounts.append(amount)

    if any(row["activity"] not in {"0", "1"} for row in campaigns):
        raise ValueError("Campaign activity must be 0 or 1")
    activity = {parse_date(row["date"]): int(row["activity"]) for row in campaigns}
    if len(activity) != len(campaigns):
        raise ValueError("Campaign dates must be unique")
    calendar = sorted(activity)
    if (calendar[-1] - calendar[0]).days + 1 != len(calendar):
        raise ValueError("Campaign calendar must include every day")
    if calendar[0] != parse_date(calendar_bounds["start"]) or calendar[-1] != parse_date(
        calendar_bounds["end"]
    ):
        raise ValueError("Campaign calendar does not match reference bounds")
    if not daily_rows.keys() <= activity.keys():
        raise ValueError("Sales dates are outside the campaign calendar")
    campaign_days = [day for day, value in activity.items() if value > 0]
    if len(campaign_days) != 1:
        raise ValueError("Expected one campaign impulse in exported activity")
    if (
        len(lag_search_days) != 2
        or not all(type(value) is int for value in lag_search_days)
        or not 0 <= lag_search_days[0] < lag_search_days[1] < len(calendar) - 1
    ):
        raise ValueError("Invalid lag search interval")

    correlations = {}
    for lag in range(lag_search_days[0], lag_search_days[1] + 1):
        shift = timedelta(days=lag)
        paired_days = [day for day in calendar if day + shift in activity]
        correlations[lag] = correlation(
            [activity[day] for day in paired_days],
            [daily_rows[day + shift] for day in paired_days],
        )
    best_score = max(correlations.values())
    best_lags = [lag for lag, score in correlations.items() if abs(score - best_score) < 1e-12]
    if best_score <= 0 or len(best_lags) != 1:
        raise ValueError("Campaign response lag is ambiguous")
    lag = best_lags[0]
    response_day = campaign_days[0] + timedelta(days=lag)
    peak_month = min(monthly_rows, key=lambda month: (-monthly_rows[month], month))
    revenue_month = min(monthly_revenue, key=lambda month: (-monthly_revenue[month], month))
    return {
        "total_rows_in_sales": len(sales),
        "duplicate_rows": len(sales)
        - len({tuple(row[col] for col in SALES_COLUMNS) for row in sales}),
        "missing_quantity_rows": sum(row["quantity"] in {"", "-", "n/a"} for row in sales),
        "seasonality_peak_month": peak_month,
        "seasonality_peak_rows": monthly_rows[peak_month],
        "revenue_peak_month": revenue_month,
        "revenue_peak_amount": str(monthly_revenue[revenue_month].quantize(Decimal("0.01"))),
        "lag_days": lag,
        "lag_spike_date": response_day.strftime("%d.%m.%Y"),
        "lag_spike_rows": daily_rows[response_day],
        "anomalies_max_amount": str(max(amounts).quantize(Decimal("0.01"))),
    }


def verify(data_dir: Path, expectations_path: Path) -> dict[str, int | str]:
    expectations = json.loads(expectations_path.read_text(encoding="utf-8"))
    references = expectations["metrics"]
    if set(references) != METRIC_NAMES:
        raise ValueError("Incomplete or unknown reference metrics")
    for name, reference in references.items():
        for field in ("measure", "aggregation", "unit", "description", "population"):
            if not reference.get(field):
                raise ValueError(f"{name}: missing {field}")
        if reference["population"] not in expectations["populations"]:
            raise ValueError(f"{name}: unknown population policy")
    actual = calculate_metrics(data_dir, expectations["lag_search_days"], expectations["calendar"])
    mismatches = [
        f"{name}: expected {reference['expected_value']!r}, got {actual[name]!r}"
        for name, reference in references.items()
        if actual[name] != reference["expected_value"]
    ]
    if mismatches:
        raise ValueError("\n".join(mismatches))
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("tests/fixtures/synthetic"))
    parser.add_argument(
        "--expectations", type=Path, default=Path("eval/synthetic_expectations.json")
    )
    args = parser.parse_args()
    try:
        actual = verify(args.data_dir, args.expectations)
    except (OSError, ValueError, KeyError, ArithmeticError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "metrics": actual}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
