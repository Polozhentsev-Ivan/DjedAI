import os

import numpy as np
import pandas as pd

np.random.seed(42)
os.makedirs("tests/fixtures/synthetic", exist_ok=True)

# 1. Магазины (ID с ведущими нулями - проверка для Андрея)
stores = pd.DataFrame(
    {
        "store_id": [f"{i:03d}" for i in range(1, 11)],  # "001", "002"...
        "city": ["Москва", "СПб", "Казань", "Новосибирск", "Екатеринбург"] * 2,
        "region": ["Центр", "Северо-Запад", "Поволжье", "Сибирь", "Урал"] * 2,
    }
)
stores.to_csv("tests/fixtures/synthetic/stores.csv", index=False)

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
products.to_csv("tests/fixtures/synthetic/products.csv", index=False)

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
        qty_str = str(quantity) if np.random.rand() > 0.05 else np.random.choice(["-", "n/a", ""])

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

# Дубли (5%)
duplicates = sales_df.sample(frac=0.05)
sales_df = pd.concat([sales_df, duplicates], ignore_index=True)
sales_df["sale_id"] = range(1, len(sales_df) + 1)

sales_df.to_csv("tests/fixtures/synthetic/sales.csv", index=False)

print("Данные сгенерированы!")
print(f"Всего строк в продажах (с дублями): {len(sales_df)}")
print("Файлы готовы. Ожидания нужно обновить в eval/synthetic_expectations.json")
