# Backend

FastAPI, API и SSE, хранение метаданных приложения и миграции, чаты, реестр результатов, дашборды, пользователи, сессии и проверки доступа.

## Структура

- `config.py` — конфигурация приложения (`pydantic-settings`), чтение `.env`, безопасные дефолты, путь к хранилищу результатов `RESULTS_DATA_DIR` и явный dev allowlist для CORS (`BACKEND_CORS_ORIGINS`, по умолчанию `http://localhost:5173,http://127.0.0.1:5173`; пустая строка отключает доступ сторонним источникам, wildcard запрещен);
- `app.py` — фабрика FastAPI-приложения `create_app()`, CORS, обработка ошибок, `GET /health`, полная схема OpenAPI со всеми контрактами и SSE (`GET /openapi.json`, `GET /api/openapi.json`) и реестр схем `GET /api/schemas`;
- `api/` — роутеры с префиксом `/api` (`health`, `datasets`, `chats`, `results`, `dashboards`, `auth`);
- `chat_service.py` — оркестрация хода чата: сохранение вопроса, запуск графа агента, преобразование блоков, сохранение результата и истории;
- `sse.py` — кодирование событий агента в wire-формат `text/event-stream`;
- `db/` — подсистема постоянного хранения приложения:
  - `base.py` — базовый декларативный класс `Base`;
  - `session.py` — фабрика сессий `get_db()`, создание движка `create_db_engine()` с поддержкой `PRAGMA foreign_keys = ON` для SQLite;
  - `models.py` — SQLAlchemy-модели сущностей со связями владения (`users`, `sessions`, `datasets`, `chats`, `messages`, `results`, `dashboards`);
  - `repositories.py` — сохранение и чтение вызовов инструментов (`ToolResult` со статусами `ok`, `error`, `empty`), потоковый сброс больших табличных данных в Parquet через DuckDB по ссылке `data_ref`, CRUD-операции сущностей;
- `auth/` — Argon2id-хеширование паролей и постоянные сессии с SHA-256 verifier токена, сроком действия и отзывом (AUTH-1); HTTP-роутеры пока остаются заглушками AUTH-2;
- `migrations/` — среда и версионированные миграции Alembic. Применение: `uv run --locked alembic upgrade head`. `downgrade base` удаляет всё хранилище и применяется только к временной тестовой БД.

БД приложения по умолчанию — SQLite (`sqlite:///./var/app.db`); пользователи и сессии используют её вместе с метаданными. DuckDB используется отдельно для аналитики. Сравнение СУБД, контракт сессий, миграции и условия перехода на PostgreSQL описаны в [решении AUTH-1](../../../docs/decisions/101-auth-storage.md).

Загрузка и профилирование вызываются через интерфейс `data/`, расчёты — через `analytics/`, работа агента — через `agent/`. Общие модели берутся из `contracts/`. Для генерации TypeScript-типов frontend схема экспортируется через `python scripts/export_schema.py`.
