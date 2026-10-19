# Вспомогательные команды

Воспроизводимые команды генерации синтетики, подготовки демо-данных и другой вспомогательной работы по задачам проекта. Для каждой команды указываем назначение, вход, выход и способ запуска.

Python-команды запускаем в среде проекта через uv. Серверная логика приложения остаётся в `src/djedai/`, браузерный интерфейс — в [frontend/](../frontend/README.md); отчёты и выгрузки — вне исходного кода. Текущие `check-repository.mjs` и его тесты проверяют структуру, UTF-8, локальные ссылки и запрет сгенерированных файлов. Frontend npm scripts и его CI появятся в B4-1 / #83 вместе с рабочим каркасом.

## Команды

- `generate_synthetic_data.py` — генерация синтетических CSV-файлов магазинов, товаров и продаж с аномалиями и сезонностью в `tests/fixtures/synthetic/`.
  - Запуск: `uv run python scripts/generate_synthetic_data.py`
- `export_schema.py` — экспорт полной схемы OpenAPI и отдельных JSON Schema контрактов для генерации TypeScript-типов frontend (#92, #83).
  - Проверка схемы: `uv run python scripts/export_schema.py --check`
  - Экспорт OpenAPI в файл: `uv run python scripts/export_schema.py -o openapi.json`
  - Вывод команды кодогенерации TS: `uv run python scripts/export_schema.py --ts-command`

Локальные скрипты публикации issues из родительской рабочей папки сюда не перенесены: это отдельные инструменты подготовки бэклога.
