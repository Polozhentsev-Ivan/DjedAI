# Backend

FastAPI, API и SSE, хранение метаданных приложения и миграции, чаты, реестр результатов, дашборды, пользователи, сессии и проверки доступа.

## Структура

- `config.py` — конфигурация приложения (`pydantic-settings`), чтение `.env`, безопасные дефолты и явный dev allowlist для CORS (`BACKEND_CORS_ORIGINS`, по умолчанию `http://localhost:5173,http://127.0.0.1:5173`; пустая строка отключает доступ сторонним источникам, wildcard запрещен);
- `app.py` — фабрика FastAPI-приложения `create_app()`, CORS, обработка ошибок, `GET /health`, полная схема OpenAPI со всеми контрактами и SSE (`GET /openapi.json`, `GET /api/openapi.json`) и реестр схем `GET /api/schemas`;
- `api/` — роутеры с префиксом `/api` (`health`, `datasets`, `chats`, `results`, `dashboards`, `auth`);
- `db/` — подключение SQLAlchemy, фабрика сессий `get_db()`, базовый класс моделей `Base`;
- `migrations/` — среда и версионированные миграции Alembic.

Загрузка и профилирование вызываются через интерфейс `data/`, расчёты — через `analytics/`, работа агента — через `agent/`. Общие модели берутся из `contracts/`. Для генерации TypeScript-типов frontend схема экспортируется через `python scripts/export_schema.py`.
