import os

import numpy as np
import pandas as pd

# Фиксируем seed, чтобы данные генерировались одинаково при каждом запуске (воспроизводимость)
np.random.seed(42)

# Создаем папку для данных, если её нет
os.makedirs("tests/fixtures/synthetic", exist_ok=True)

# 1. Таблица МАГАЗИНЫ (10 штук)
stores = pd.DataFrame(
    {
        "store_id": range(1, 11),
        "city": ["Москва", "СПб", "Казань", "Новосибирск", "Екатеринбург"] * 2,
        "region": ["Центр", "Северо-Запад", "Поволжье", "Сибирь", "Урал"] * 2,
    }
)
stores.to_csv("tests/fixtures/synthetic/stores.csv", index=False)

# 2. Таблица ТОВАРЫ (50 штук)
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

# 3. Таблица ПРОДАЖИ (за 1 год - 2023)
dates = pd.date_range(start="2023-01-01", end="2023-12-31", freq="D")
sales = []

# Закладываем ЛАГ 5 ДНЕЙ (например, после рекламной кампании 1 июня, всплеск продаж 6 июня)
campaign_date = pd.Timestamp("2023-06-01")
lag_date = campaign_date + pd.Timedelta(days=5)

for date in dates:
    # Количество продаж в день (в среднем 10)
    num_sales = np.random.poisson(10)

    # Закладываем СЕЗОННОСТЬ 12 МЕСЯЦЕВ (в декабре продаж в 3 раза больше)
    if date.month == 12:
        num_sales = int(num_sales * 3)

    # Закладываем ЛАГ (в день X+5 продаж в 5 раз больше)
    if date == lag_date:
        num_sales = int(num_sales * 5)

    for _ in range(num_sales):
        # Иногда закладываем АНОМАЛИИ (выбросы) - 1% случаев
        is_anomaly = np.random.rand() < 0.01
        quantity = 1000 if is_anomaly else np.random.randint(1, 5)

        product_id = np.random.randint(1, 51)
        price = products.loc[products["product_id"] == product_id, "price"].values[0]

        sales.append(
            {
                "sale_id": len(sales) + 1,
                "date": date.strftime("%Y-%m-%d"),
                "store_id": np.random.randint(1, 11),
                "product_id": product_id,
                "quantity": quantity,
                "total_amount": round(quantity * price, 2),
            }
        )

sales_df = pd.DataFrame(sales)

# Закладываем ДУБЛИ (берем 5% случайных строк и добавляем их в конец)
duplicates = sales_df.sample(frac=0.05)
sales_df = pd.concat([sales_df, duplicates], ignore_index=True)
sales_df["sale_id"] = range(1, len(sales_df) + 1)  # Пересчитываем ID

sales_df.to_csv("tests/fixtures/synthetic/sales.csv", index=False)

# --- РАССЧИТЫВАЕМ ЭТАЛОННЫЕ ЗНАЧЕНИЯ (Независимо от SQL) ---
print("Данные сгенерированы!")
print(f"Всего строк в продажах (с дублями): {len(sales_df)}")
print(f"Максимальная сумма одной покупки (аномалия): {sales_df['total_amount'].max()}")
print(
    f"Месяц с самой большой выручкой: {
        pd.to_datetime(sales_df['date']).dt.month.value_counts().idxmax()
    } (декабрь - 12)"
)
