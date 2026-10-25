# Локальный запуск CP1

Из корня репозитория создайте `.env` из `.env.example`, заполните `MODEL_API_KEY` и выполните:

```sh
docker compose up --build -d --wait
```

Работающий Docker Linux engine обязателен. UI: [http://localhost:8080](http://localhost:8080). Подробный пользовательский сценарий и повторный запуск — в [корневом README](../README.md#запуск-cp1-через-compose).

## Сервисы и постоянные данные

| Сервис | Назначение |
| --- | --- |
| `backend` | FastAPI, CSV/DuckDB, агент и SQLite/Parquet. Entrypoint выполняет `uv run --locked alembic upgrade head`, bootstrap владельца и затем uvicorn. Healthcheck ждёт `/health`. |
| `frontend` | Отдельный Node-этап с `npm ci` и `npm run build`; готовую статику раздаёт nginx. `/api/` проксируется в backend без изменения пути. |
| `app-data` | Именованный том `/app/var`: app.db, CSV, DuckDB, каталог и результаты Parquet. Сохраняется после `restart`/`down`. |

App-db на CP1 — файл SQLite в том же backend; отдельный сервер БД не требуется. Модель — согласованный внешний DeepInfra/Qwen3.6-35B-A3B. Демо-БД PostgreSQL/ClickHouse — в профиле `demo` ([ниже](#демо-бд-postgresql-и-clickhouse)); финальная многопользовательская конфигурация — #99 и последующие задачи.

Compose задаёт `CP1_LOCAL_MODE=false`: bootstrap прежнего CP1-владельца пропускается, все ресурсные API требуют Bearer-сессии. Старые данные в томе сохраняются за прежним владельцем. В Compose все пути данных закреплены внутри `/app/var`; изменения локальных `APP_DATABASE_URL`/`*_DATA_DIR` из `.env` на них не влияют. PostgreSQL DSN шифруется серверным `SOURCE_ENCRYPTION_KEY` из `.env`; сохраняйте ключ отдельно от резервной копии тома, иначе подключения нельзя восстановить. Формы входа подключаются в #104; автоматический smoke использует API-вход и переменные `DJEDAI_SMOKE_USERNAME`/`DJEDAI_SMOKE_PASSWORD`.

## Закреплённые сборки

В Dockerfile зафиксированы версия и digest индекса каждого внешнего образа. Доступность тегов и digest проверены через Docker Registry HTTP API / GHCR 20.10.2026.

| Образ | Согласование |
| --- | --- |
| `python:3.13.16-slim-bookworm` | `.python-version`, `requires-python` |
| `ghcr.io/astral-sh/uv:0.13.1` | версия uv в CI |
| `node:24.22.0-bookworm-slim` | `.nvmrc`; npm `11.19.1` проверяется перед `npm ci` |
| `nginx:1.30.0-alpine` | раздача статики и reverse proxy |

Python использует `uv sync --locked --no-dev`; `PYTHONPATH=/app/src` соответствует src-layout без установки пакета. Entrypoint запускает команды через `uv run --locked --no-sync`: зависимости уже установлены при сборке и при старте не меняются. Backend работает от отдельного пользователя, имеющего права на том. Frontend build не принимает `.env` и `MODEL_*`: `.dockerignore` допускает только входы сборки и повторно исключает локальные env/data/build-файлы. Production build задаёт публичный `/api` и `VITE_API_MOCKS=false`; MSW остаётся только в dev-пути.

## Proxy и проверка

`nginx.conf` сохраняет `Authorization` и multipart body/boundary, не ограничивает upload стандартным nginx-лимитом 1 MiB, отключает request/response buffering и gzip для `/api/`. Таймаут чтения потока — 600 секунд. Для вложенных UI-маршрутов действует `try_files … /index.html`.

```sh
docker compose config --quiet
docker compose ps
curl --fail http://localhost:8080/api/health
curl --fail http://localhost:8080/chat
```

Проверьте upload через UI и в DevTools Network: `POST /api/datasets/upload` должен идти с multipart `files`, затем события обработки и готовый каталог. При отправке вопроса `POST …/messages` должен выдавать SSE со статусами до итогового ответа. Для прямой проверки потока используйте `curl -N` и фактические ID из UI; POST вопроса не повторяйте автоматически после разрыва.

После `docker compose restart` прежние URL чата/каталога и ID результата должны открывать сохранённые объекты. Ошибки контейнеров: `docker compose logs --tail=100 backend frontend`. Если Docker daemon недоступен, проверка `compose config` подтверждает лишь конфигурацию, а не сборку или сквозной запуск.

## Compose с управляемой моделью для CI

Проверка тех же production-образов без внешнего inference использует отдельный overlay. На Docker-хосте из корня репозитория запустите тестовый OpenAI-совместимый сервер:

```sh
uv run --locked uvicorn --app-dir tests/e2e serve_cp1:model_app --factory --host 0.0.0.0 --port 18081
```

В другом терминале, с `.env`, созданным из безопасного образца:

```sh
docker compose -f compose.yaml -f infra/compose.test.yaml up --build -d --wait
```

Overlay подставляет исключительно тестовые `MODEL_*` в backend и явно связывает `host.docker.internal` с Docker host gateway для Linux CI. Все HTTP-запросы приложения по-прежнему проходят через nginx, SQLite/DuckDB/Parquet используют обычный том. Этот тест не является проверкой выбранной реальной модели; её прогон выполняется отдельно без overlay.

## Демо-БД: PostgreSQL и ClickHouse

Для показа подключений (T1, #100) профиль `demo` добавляет сервисы `demo-postgres` и `demo-clickhouse`. В обеих БД один и тот же датасет: синтетика seed=42 из [tests/fixtures/synthetic](../tests/fixtures/synthetic/), пропущенная через приём DjedAI (`csv_ingest` → typed views), то есть одинаковые таблицы, колонки, типы и значения. Без `--profile demo` эти сервисы не запускаются, обычный запуск не меняется.

### Наполнение с нуля

1. В `.env` задайте шесть паролей `DEMO_PG_*_PASSWORD` и `DEMO_CH_*_PASSWORD`. Роли создаются из них только при первом старте пустого тома (`infra/postgres/init-demo.sh` → [roles.sql](postgres/roles.sql), `infra/clickhouse/init-demo.sh` → [roles.sql](clickhouse/roles.sql)). Чтобы сменить пароли, пересоздайте тома (см. ниже).
2. Там же заполните адреса для скриптов (пароли в URL кодируются `%XX`):

   ```sh
   DJEDAI_DEMO_PG_LOADER_DSN=postgresql://djedai_loader:<пароль>@127.0.0.1:55432/djedai_demo
   DJEDAI_DEMO_PG_READER_DSN=postgresql://djedai_reader:<пароль>@127.0.0.1:55432/djedai_demo
   DJEDAI_DEMO_CH_LOADER_URL=http://djedai_loader:<пароль>@127.0.0.1:58123
   DJEDAI_DEMO_CH_READER_URL=http://djedai_reader:<пароль>@127.0.0.1:58123
   ```

3. Запустите БД, наполните их и сверьте контрольные значения:

   ```sh
   docker compose --profile demo up -d --wait demo-postgres demo-clickhouse
   set -a; . ./.env; set +a   # скрипты читают адреса из окружения
   uv run --locked python scripts/seed_demo_postgres.py
   uv run --locked python scripts/seed_demo_clickhouse.py
   uv run --locked python scripts/verify_demo_db.py
   ```

   `docker compose --profile demo up -d --wait` без имён сервисов поднимает и приложение вместе с демо-БД.

Seed можно повторять в любой момент: таблицы пересоздаются. `verify_demo_db.py` под ролями `djedai_reader` сверяет состав таблиц, колонки, число строк, агрегаты из [eval/demo_db_expectations.json](../eval/demo_db_expectations.json) и отказ читателю в записи; печатает таблицу по каждой БД и завершается кодом 0 (`status: passed`) или 1. С одним из адресов (`--pg-dsn` или `--ch-url`) проверяется только эта БД.

Полное пересоздание затрагивает только тома демо-БД; не используйте `down -v`, он удалит и `app-data`:

```sh
docker compose --profile demo rm -sfv demo-postgres demo-clickhouse
docker volume rm djedai_demo-pg-data djedai_demo-ch-data
```

### Параметры подключения

| | PostgreSQL | ClickHouse |
| --- | --- | --- |
| Образ | `postgres:17.11-bookworm` (digest в `compose.yaml`) | `clickhouse/clickhouse-server:26.3.60.2` (LTS, digest в `compose.yaml`) |
| С хоста | `127.0.0.1:${DEMO_PG_PORT:-55432}` | HTTP `127.0.0.1:${DEMO_CH_HTTP_PORT:-58123}`; native-порт не публикуется |
| Из backend | `demo-postgres:5432` | `demo-clickhouse:8123` |
| База | `djedai_demo`, схема `public` | `djedai_demo` |
| Загрузка | `djedai_loader` — владелец таблиц | `djedai_loader` — все права только на `djedai_demo.*` |
| Подключение в DjedAI | `djedai_reader`: `SELECT`, `default_transaction_read_only = on` | `djedai_reader`: `SELECT` на `djedai_demo.*`, `readonly = 2` (CONST) — лимиты запроса задавать можно, запись и снятие `readonly` нет |
| Администратор | `postgres` / `DEMO_PG_ADMIN_PASSWORD` | `djedai_admin` / `DEMO_CH_ADMIN_PASSWORD` |

В UI или `POST /api/datasets/connect` PostgreSQL подключается строкой `postgresql://djedai_reader:<пароль>@demo-postgres:5432/djedai_demo`. ClickHouse — `"kind":"clickhouse"` и строкой `http://djedai_reader:<пароль>@demo-clickhouse:8123/djedai_demo` ([#61](../docs/decisions/61-clickhouse.md)). Реальная приёмка подключения в одноразовом контейнере: `uv run --locked python scripts/test_clickhouse_isolation.py` (нужен Docker).

### Контрольные значения

| Таблица | Строк | Колонки |
| --- | --- | --- |
| `campaigns` | 365 | `date`, `activity` |
| `products` | 50 | `product_id`, `product_name`, `category`, `price` |
| `sales` | 4 376 | `sale_id`, `date`, `store_id`, `product_id`, `quantity`, `discount_pct`, `total_amount` |
| `stores` | 10 | `store_id`, `city`, `region` |

| Проверка | Значение |
| --- | --- |
| `sum(total_amount)` по `sales` | 1 268 254 502,40 RUB |
| Полные дубли строк `sales` | 208 |
| `quantity IS NULL` | 210 |
| Даты продаж | 01.01.2023 — 31.12.2023 |
| Месяц с наибольшим числом строк | декабрь, 861 |
| Месяц с наибольшей суммой | апрель, 253 427 853,81 RUB |
| Различных `store_id` / `product_id` в продажах | 10 / 50 |
| JOIN `sales` со `stores` и с `products` | 4 376 строк, сумма не меняется |
| `round(sum(price), 2)` по `products` | 1 290 249,22 |
| `sum(activity)` / дней в `campaigns` | 1 / 365 |

Источник истины — JSON с SQL каждой проверки; значения со ссылкой `reference` совпадают с [eval/synthetic_expectations.json](../eval/synthetic_expectations.json), а unit-тест `tests/unit/data/test_demo_db_seed.py` пересчитывает все проверки по CSV без сервера БД.

### PostgreSQL раньше полного профиля

PG-часть готова с B1-8 / #60 и не требует ClickHouse: `docker compose --profile demo up -d --wait demo-postgres`, `seed_demo_postgres.py`, затем `verify_demo_db.py --pg-dsn "$DJEDAI_DEMO_PG_READER_DSN"`. Установка без Docker (Fedora) — в [postgres/](postgres/README.md).

Интеграционные тесты на наполненных БД (без переменных пропускаются):

```sh
DJEDAI_TEST_PG_DSN="$DJEDAI_DEMO_PG_READER_DSN" DJEDAI_TEST_CH_URL="$DJEDAI_DEMO_CH_READER_URL" \
  uv run --locked pytest --strict-markers tests/integration/test_demo_databases.py tests/integration/test_postgres_adapter.py
```
