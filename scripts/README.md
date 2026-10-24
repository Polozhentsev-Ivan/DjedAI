# Вспомогательные команды

## Минимальный сквозной прогон CP1 без UI

Описание проверок и результат запуска на 500 000 записей — в
[протоколе прогона](../docs/cp1-smoke-run.md).

`smoke_cp1.py` требует код API чата из PR #139. Он вызывает `stream_chat_turn()`
напрямую: CSV → типизация → профиль/каталог → LangGraph с реальной моделью →
SQL по загруженным данным → SQLite/Parquet → проверка в новом процессе.
HTTP-сервер, UI, Compose и регистрация не нужны; используется отдельный тестовый владелец.

По умолчанию скачивается полный `08_Фоновые_задания.csv` (122 084 672 байта,
500 000 записей) из [HK_Datasets](https://gitverse.ru/kban/HK_Datasets),
ревизия `716fbbcdc3dfa2a63dba1cf835fcc780c5e91a1d`.
Докачка идёт по HTTP Range, Git blob проверяется; исходник кэшируется в
`artifacts/hk-datasets/`, SHA-256 записывается в отчёт. CSV и результаты не коммитятся.

В окружении или локальном `.env` нужен `DEEPINFRA_API_KEY` либо `MODEL_API_KEY`.
Секрет в командную строку передавать не требуется. Используются
[DeepInfra Chat Completions](https://docs.deepinfra.com/chat/overview):
`https://api.deepinfra.com/v1/openai`, модель
[`Qwen/Qwen3.6-35B-A3B`](https://deepinfra.com/Qwen/Qwen3.6-35B-A3B) (35B суммарных параметров).
Модель возвращает типизированные JSON-ответы; ключ безуспешным ответом заглушки не заменяется.
Адрес и модель можно изменить через `--base-url` и `--model`; из `.env` скрипт берёт ключ,
`MODEL_TIMEOUT_SECONDS` и `MODEL_MAX_OUTPUT_TOKENS`.

Из корня репозитория:

```sh
uv run --locked python scripts/smoke_cp1.py
```

Каждый запуск создаёт отдельный каталог `artifacts/cp1-smoke/<UTC-время>-<суффикс>/`.
Там находятся `report.json`, `catalog.json`, `model_calls.jsonl`, `events.jsonl`,
`app.db`, `data.duckdb` и `results/*.parquet`. В отчёте — вопрос, ответ, SQL,
`chat_id`, `message_id`, `result_ids`, источник и результаты повторного чтения.

Успешный прогон завершается кодом 0 и `status: passed`. Он требует вызова `run_sql`
с прямым `COUNT(*)` всей таблицы датасета без CTE, фильтров и JOIN, числа из
независимого Python CSV-парсера, такого же числа в текстовом ответе и сохранности
каталога, сообщений, результата и данных
после открытия отдельным Python-процессом. Ошибка модели, пустой ответ,
неверное число, SQL с одной константой или подменой таблицы через CTE дают код 1.

Дополнительные режимы:

```sh
# Подготовить данные без вызова модели; status: prepared не является LLM-приёмкой.
uv run --locked python scripts/smoke_cp1.py --prepare-only

# Использовать свой CSV с заголовком или другой файл настроек.
uv run --locked python scripts/smoke_cp1.py --csv /path/data.csv --env-file /path/.env

# Повторить только проверку сохранённых данных, без API модели.
uv run --locked python scripts/smoke_cp1.py --verify-only artifacts/cp1-smoke/<run>/report.json
```

Проверки CLI с локальным HTTP endpoint модели (без внешней сети и настоящего ключа):

```sh
uv run --locked pytest tests/integration/test_cp1_smoke.py -q
```

Этот сценарий подтверждает ядро CP1. Браузерный транспорт, proxy, Compose и полная
приёмка #106 проверяются отдельно.

Воспроизводимые команды генерации синтетики, подготовки демо-данных и другой вспомогательной работы по задачам проекта. Для каждой команды указываем назначение, вход, выход и способ запуска.

Python-команды запускаем в среде проекта через uv. Серверная логика приложения остаётся в `src/djedai/`, браузерный интерфейс — в [frontend/](../frontend/README.md); отчёты и выгрузки — вне исходного кода. Текущие `check-repository.mjs` и его тесты проверяют структуру, UTF-8, локальные ссылки и запрет сгенерированных файлов. Frontend npm scripts и его CI появятся в B4-1 / #83 вместе с рабочим каркасом.

## Команды

- `verify_golden.py` — проверка [golden-набора](../eval/README.md) для сценариев 1–10 и edge cases (#79/#80). Проверяет минимумы всех групп, контекст, рубрики графиков/дашбордов и отдельно ошибки ввода. Сверяет закреплённый полный CSV HK_Datasets по размеру/SHA-256/Git blob и независимо пересчитывает группировки, корреляции, лаги и условные прогнозы средствами стандартной библиотеки Python.
  - Только структура, без сети и CSV: `uv run --locked python scripts/verify_golden.py --validate-only`.
  - Проверка полного файла: `uv run --locked python scripts/verify_golden.py --data-file "../artifacts/golden-v1/08_Фоновые_задания.csv" --download --output ../artifacts/golden-v1/reference-report.json`.
  - `--download` скачивает закреплённую версию только при отсутствии файла; существующий кэш проверяется и не заменяется. Для локального файла флаг можно опустить.
  - Кэш CSV и JSON-отчёт должны находиться вне checkout. Статусы `structure_valid`, `verified` и `reference_mismatch` различаются; `agent_evaluation` остаётся `not_run`. Эта команда проверяет эталоны и не заменяет будущий eval агента #81.

- `benchmark_model_profiles.py` — реальная серия Understand/Plan/Compose через API без пользовательских данных: отдельно считает валидность схем и корректность ответа/SQL, latency и prompt tokens. Ошибочный план записывает как неуспех, не обрывая серию. JSON-отчёт сохраняется локально в `artifacts/model-profiles/`.
  - Запуск: `uv run --locked python scripts/benchmark_model_profiles.py --model Qwen/Qwen3.6-35B-A3B --repeats 3 --context-lines 200`
  - Ключ: `DEEPINFRA_API_KEY` либо `MODEL_API_KEY` из окружения/локального `.env`; в отчёт он не включается.
  - Сравнение: повторить `--model` для каждого профиля. `--base-url`, `--timeout`, `--max-output-tokens`, `--context-lines` задают параметры замера. Ограничение проекта — открытые веса до 80B суммарных параметров.
  - Результаты и ограничения выбранного профиля: [протокол PL-1](../docs/model-profile-pl1.md). Это минимальная диагностика, не полный eval #81.

- `generate_synthetic_data.py` — воспроизводимая генерация CSV магазинов, товаров, продаж и ежедневной рекламной активности с seed=42 в `tests/fixtures/synthetic/`.
  - Запуск: `uv run python scripts/generate_synthetic_data.py`
  - В другую папку: `uv run python scripts/generate_synthetic_data.py --output-dir /tmp/djedai-synthetic`
  - `campaigns.csv`: `date` в формате `dd.mm.yyyy`, `activity` — 1 в день рекламного импульса, 0 в остальные дни. Отклик вычисляется по дневному числу записей в `sales.csv`.
  - Полные копии продаж сохраняют исходный `sale_id`; ключ дубля — все колонки, число лишних копий — 208 (5% от 4168 исходных записей с округлением). Генератор не меняет эталоны автоматически.
- `verify_synthetic_data.py` — независимая сверка CSV с `eval/synthetic_expectations.json` средствами стандартной библиотеки Python, без аналитических функций приложения.
  - Сверяются метрики (`metrics`) и связи таблиц (`relations`): для ожидаемых связей — overlap, Jaccard, кратность, число строк и суммы `total_amount` до и после JOIN, для похожих ID без связи (`not_expected`) — меры пересечения.
  - Проверка сохранённых CSV: `uv run python scripts/verify_synthetic_data.py`
  - Проверка другой папки: `uv run python scripts/verify_synthetic_data.py --data-dir /tmp/djedai-synthetic`
  - Лаг определяется максимумом корреляции рекламной активности и дневного числа продаж при сдвигах 0–14 дней. Источник сигнала — CSV; эталонный лаг не используется в расчёте.
  - Сезонность измеряется числом сырых записей: декабрь, 861 строка. Денежный максимум отдельно: апрель, 253 427 853,81 RUB. Суммы считаются через `Decimal`, до скидки; дубли, пропуски количества и аномалии включены. Это эталоны до очистки; меры, агрегации, единицы и политики перечислены в JSON.
  - Код выхода 0 — эталоны совпали, 1 — несовпадение или неполные входные данные. В тестах две генерации во временные папки побайтово сравниваются с сохранёнными CSV.
- `seed_demo_postgres.py`, `seed_demo_clickhouse.py` — загрузка синтетики из `tests/fixtures/synthetic/` в демо-PostgreSQL и демо-ClickHouse (#100). Обе используют `demo_dataset.py`: тот же разбор CSV и typed views, что в DjedAI, поэтому датасет в двух БД один. Повторный запуск пересоздаёт таблицы.
  - PostgreSQL: `uv run python scripts/seed_demo_postgres.py --dsn 'postgresql://djedai_loader:<пароль>@127.0.0.1:55432/djedai_demo'` (или `DJEDAI_DEMO_PG_LOADER_DSN`); запись через DuckDB `ATTACH`.
  - ClickHouse: `uv run python scripts/seed_demo_clickhouse.py --url 'http://djedai_loader:<пароль>@127.0.0.1:58123'` (или `DJEDAI_DEMO_CH_LOADER_URL`); таблицы MergeTree с типами из DuckDB, данные — `INSERT … FORMAT Parquet` в HTTP-интерфейс, без клиентской библиотеки и адаптера #61.
  - Запуск БД, роли и параметры — [infra/README.md](../infra/README.md#демо-бд-postgresql-и-clickhouse); установка PostgreSQL без Docker — [infra/postgres/README.md](../infra/postgres/README.md).
- `verify_demo_db.py` — сверка наполненных демо-БД с [eval/demo_db_expectations.json](../eval/demo_db_expectations.json) под ролями `djedai_reader`: таблицы, колонки, строки, агрегаты и запрет записи. Сначала проверяет, что эталоны совпадают с `synthetic_expectations.json`.
  - Обе БД: `uv run python scripts/verify_demo_db.py` с `DJEDAI_DEMO_PG_READER_DSN` и `DJEDAI_DEMO_CH_READER_URL`; одна БД — только `--pg-dsn` или `--ch-url`.
  - Код 0 и `status: passed` — все проверки совпали; 1 — расхождение, ошибка запроса или подключения. Пароли в вывод не попадают.
- `test_postgres_isolation.py` — воспроизводимая приёмка PostgreSQL и изоляции владельцев (#60, #103). Нужны Docker с Linux engine и среда проекта; для первой загрузки образа и расширения DuckDB `postgres` нужен доступ к их реестрам. LLM не запускается: модельные ответы в тестах заданы явно, а auth, HTTP/SSE, граф, инструменты, SQLite, Parquet и PostgreSQL работают через код приложения.
  - Запуск из корня репозитория: `uv run --locked python scripts/test_postgres_isolation.py`.
  - С JSON-отчётом вне checkout: `uv run --locked python scripts/test_postgres_isolation.py --output ../artifacts/issue-103/postgres-acceptance.json`.
  - Образ закреплён точным тегом и digest: `postgres:17.11-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27`. Скрипт создаёт отдельный контейнер с уникальным именем и меткой, публикует порт только на `127.0.0.1` и загружает синтетику в две БД с разным числом магазинов. Пароли и ключ шифрования случайные, живут только во время прогона; DSN не передаются аргументами процесса и не записываются в отчёт.
  - Проверяются адаптер PostgreSQL, сохранение зашифрованного источника, чтение живых данных без копирования, read-only доступ, два владельца и одинаковые имена таблиц в разных источниках, чужие `404`, запросы и результаты после перезапуска, безопасные ошибки подключения. Запускаются `test_postgres_adapter.py`, `test_owned_postgres.py` и `test_postgres_owner_api.py`.
  - Код 0 и `status: passed` означают, что все тесты прошли без пропусков и контейнер удалён. При завершении скрипт проверяет свою метку и удаляет только созданный им контейнер с его временным томом; другие контейнеры и данные Docker не затрагиваются. Неудачная проверка или неподтверждённое удаление дают код 1. Без `--output` отчёт выводится в stdout.
- `export_schema.py` — экспорт полной схемы OpenAPI и отдельных JSON Schema контрактов для генерации TypeScript-типов frontend (#92, #83).
  - Проверка схемы: `uv run python scripts/export_schema.py --check`
  - Экспорт OpenAPI в файл: `uv run python scripts/export_schema.py -o openapi.json`
  - Экспорт автономных JSON-схем моделей: `uv run python scripts/export_schema.py --schemas-dir contracts-schemas/`
  - Вывод команды кодогенерации TS: `uv run python scripts/export_schema.py --ts-command`

Локальные скрипты публикации issues из родительской рабочей папки сюда не перенесены: это отдельные инструменты подготовки бэклога.
