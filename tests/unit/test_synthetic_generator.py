# --------------------------------
# тестирование генератора
# --------------------------------
import subprocess
import sys
from pathlib import Path


def test_generator_creates_three_tables(tmp_path):
    # Запускаем скрипт в изолированной временной папке tmp_path
    # Но так как скрипт использует жесткие пути, мы просто проверим, что он выполняется без ошибок
    result = subprocess.run(
        [sys.executable, "scripts/generate_synthetic_data.py"], capture_output=True, text=True
    )
    # Проверяем, что скрипт отработал успешно (код 0)
    assert result.returncode == 0
    assert "Данные сгенерированы!" in result.stdout

    # Проверяем, что файлы действительно появились
    assert Path("tests/fixtures/synthetic/stores.csv").exists()
    assert Path("tests/fixtures/synthetic/products.csv").exists()
    assert Path("tests/fixtures/synthetic/sales.csv").exists()
