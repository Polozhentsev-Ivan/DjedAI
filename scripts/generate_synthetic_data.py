import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def generate_data(output_dir: Path) -> None:
    np.random.seed(42)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Магазины (ID с ведущими нулями - проверка для Андрея)
    stores = pd.DataFrame(
        {
            "store_id": [f"{i:03d}" for i in range(1, 11)],  # "001", "002"...
            "city": ["Москва", "СПб", "Казань", "Новосибирск", "Екатеринбург"] * 2,
            "region": ["Центр", "Северо-Запад", "Поволжье", "Сибирь", "Урал"] * 2,
        }
    )
    stores.to_csv(output_dir / "stores.csv", index=False, lineterminator="\n")

    # 2. Товары (Добавили процент скидки в текстовом виде)
    categories = ["Электроника", "Одежда", "Продукты", "Книги", "Бытовая химия"]
    products = pd.DataFrame(
        {
            "product_id": range(1, 51),
            "product_name": [f"Товар_{i}" for i in range(1, 51)],
            "category": np.random.choice(categories, 50),
            "price": np.round(np.random.uniform(100, 50000, 50), 2),
        }
    )
    products.to_csv(output_dir / "products.csv", index=False, lineterminator="\n")

    # 3. Продажи (Грязные данные)
    dates = pd.date_range(start="2023-01-01", end="2023-12-31", freq="D")
    sales = []

    campaign_date = pd.Timestamp("2023-06-01")
    lag_date = campaign_date + pd.Timedelta(days=5)

    for date in dates:
        num_sales = np.random.poisson(10)
        if date.month == 12:
            num_sales = int(num_sales * 3)
        if date == lag_date:
            num_sales = int(num_sales * 5)

        for _ in range(num_sales):
            is_anomaly = np.random.rand() < 0.01
            quantity = 1000 if is_anomaly else np.random.randint(1, 5)

            product_id = np.random.randint(1, 51)
            price = products.loc[products["product_id"] == product_id, "price"].values[0]
            total = round(quantity * price, 2)

            # ГРЯЗНЫЕ ДАННЫЕ:
            # 1. Дата в формате dd.mm.yyyy
            date_str = date.strftime("%d.%m.%Y")

            # 2. Деньги в формате "1 234,56 руб."
            money_str = f"{total:,.2f}".replace(",", " ").replace(".", ",") + " руб."

            # 3. Проценты ("10%", "15,5%")
            discount = np.random.choice(["0%", "5%", "10%", "15,5%"])

            # 4. Пропуски (иногда вместо числа ставим "-" или "")
            qty_str = (
                str(quantity) if np.random.rand() > 0.05 else np.random.choice(["-", "n/a", ""])
            )

            sales.append(
                {
                    "sale_id": len(sales) + 1,
                    "date": date_str,
                    "store_id": f"{np.random.randint(1, 11):03d}",  # "005"
                    "product_id": product_id,
                    "quantity": qty_str,
                    "discount_pct": discount,
                    "total_amount": money_str,
                }
            )

    sales_df = pd.DataFrame(sales)

    # Полные дубли (5%): сохраняем исходный sale_id вместе со всеми остальными полями.
    duplicates = sales_df.sample(frac=0.05)
    sales_df = pd.concat([sales_df, duplicates], ignore_index=True)

    sales_df.to_csv(output_dir / "sales.csv", index=False, lineterminator="\n")

    # Дневной рекламный сигнал: полный календарь, включая дни без активности.
    campaigns = pd.DataFrame(
        {"date": dates.strftime("%d.%m.%Y"), "activity": (dates == campaign_date).astype(int)}
    )
    campaigns.to_csv(output_dir / "campaigns.csv", index=False, lineterminator="\n")

    print("Данные сгенерированы!")
    print(f"Всего строк в продажах (с дублями): {len(sales_df)}")
    print("Проверка эталонов: python scripts/verify_synthetic_data.py --data-dir", output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация воспроизводимых синтетических CSV.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/fixtures/synthetic"),
        help="Каталог для stores.csv, products.csv, sales.csv и campaigns.csv",
    )
    args = parser.parse_args()
    generate_data(args.output_dir)


if __name__ == "__main__":
    main()
