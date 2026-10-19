# Backend

FastAPI, API и SSE, хранение метаданных приложения и миграции, чаты, реестр результатов, дашборды, пользователи, сессии и проверки доступа.

## Структура

- `config.py` — конфигурация приложения (`pydantic-settings`), чтение `.env`, безопасные дефолты, путь к хранилищу результатов `RESULTS_DATA_DIR` и явный dev allowlist для CORS (`BACKEND_CORS_ORIGINS`, по умолчанию `http://localhost:5173,http://127.0.0.1:5173`; пустая строка отключает доступ сторонним источникам, wildcard запрещен);
- `app.py` — фабрика FastAPI-приложения `create_app()`, CORS, обработка ошибок, `GET /health`, полная схема OpenAPI со всеми контрактами и SSE (`GET /openapi.json`, `GET /api/openapi.json`) и реестр схем `GET /api/schemas`;
- `api/` — роутеры с префиксом `/api` (`health`, `datasets`, `chats`, `results`, `dashboards`, `auth`);
- `db/` — подсистема постоянного хранения приложения:
  - `base.py` — базовый декларативный класс `Base`;
  - `session.py` — фабрика сессий `get_db()`, создание движка `create_db_engine()` с поддержкой `PRAGMA foreign_keys = ON` для SQLite;
  - `models.py` — SQLAlchemy-модели сущностей со связями владения (`users`, `datasets`, `chats`, `messages`, `results`, `dashboards`);
  - `repositories.py` — сохранение и чтение вызовов инструментов (`ToolResult` со статусами `ok`, `error`, `empty`), потоковый сброс больших табличных данных в Parquet через DuckDB по ссылке `data_ref`, CRUD-операции сущностей;
- `migrations/` — среда и версионированные миграции Alembic. Применение миграций: `uv run alembic upgrade head`, откат: `uv run alembic downgrade base`.

Загрузка и профилирование вызываются через интерфейс `data/`, расчёты — через `analytics/`, работа агента — через `agent/`. Общие модели берутся из `contracts/`. Для генерации TypeScript-типов frontend схема экспортируется через `python scripts/export_schema.py`.
