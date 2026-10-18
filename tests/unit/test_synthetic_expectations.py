# ----------------------------------------------------
#  проверка, что файл eval/synthetic_expectations.json
#        не сломан и содержит правильные ключи
# ----------------------------------------------------
import json
from pathlib import Path


def test_synthetic_expectations_are_valid():
    # Проверяем, что файл существует
    file_path = Path("eval/synthetic_expectations.json")
    assert file_path.exists(), "Файл с ожиданиями не найден!"

    # Читаем JSON
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    # Проверяем, что есть блок с метриками
    assert "metrics" in data

    # Проверяем, что заложены все нужные нам закономерности
    metrics = data["metrics"]
    assert "seasonality_peak_month" in metrics
    assert "lag_spike_date" in metrics
    assert "anomalies_max_amount" in metrics

    # Проверяем корректность значений
    # Проверяем корректность значений
    assert metrics["seasonality_peak_month"]["expected_value"] == 12
    # Обновили формат даты на "грязный" (dd.mm.yyyy)
    assert metrics["lag_spike_date"]["expected_value"] == "06.06.2023"
    # Здесь должна быть та цифра, которую ты получила в терминале при генерации
    # Если скрипт выдал другое число, замени его здесь!
    assert metrics["anomalies_max_amount"]["expected_value"] > 0
