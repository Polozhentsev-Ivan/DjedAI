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

App-db на CP1 — файл SQLite в том же backend; отдельный сервер БД не требуется. Модель — согласованный внешний DeepInfra/Qwen3.6-35B-A3B. Подключение demo-БД и финальная многопользовательская конфигурация остаются в #99/#100 и последующих задачах.

Compose явно включает режим одного владельца `CP1_LOCAL_MODE=true`, ID `cp1-local-user`; bootstrap безопасно повторять на существующем томе. При самостоятельном backend-запуске режим выключен по умолчанию. В Compose все пути данных закреплены внутри `/app/var`; изменения локальных `APP_DATABASE_URL`/`*_DATA_DIR` из `.env` на них не влияют.

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
