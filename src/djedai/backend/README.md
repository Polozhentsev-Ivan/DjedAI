# Backend

FastAPI, API и SSE, хранение метаданных приложения и миграции, чаты, реестр результатов, дашборды, пользователи, сессии и проверки доступа.

## Структура

- `config.py` — конфигурация приложения (`pydantic-settings`), чтение `.env`, безопасные дефолты, путь к хранилищу результатов `RESULTS_DATA_DIR` и явный dev allowlist для CORS (`BACKEND_CORS_ORIGINS`, по умолчанию `http://localhost:5173,http://127.0.0.1:5173`; пустая строка отключает доступ сторонним источникам, wildcard запрещен);
- `app.py` — фабрика FastAPI-приложения `create_app()`, CORS, обработка ошибок, `GET /health`, полная схема OpenAPI со всеми контрактами и SSE (`GET /openapi.json`, `GET /api/openapi.json`) и реестр схем `GET /api/schemas`;
- `api/` — роутеры с префиксом `/api` (`health`, `datasets`, `chats`, `results`, `dashboards`, `auth`);
- `chat_service.py` — оркестрация хода чата: сохранение вопроса, запуск графа агента, преобразование блоков, сохранение результата и истории;
- `sse.py` — кодирование событий агента в wire-формат `text/event-stream`;
- `diagnostics.py` — постоянный операторский JSONL-журнал с ротацией и CLI чтения; [формат и команды](../../../docs/agent-diagnostics.md).
- `db/` — подсистема постоянного хранения приложения:
  - `base.py` — базовый декларативный класс `Base`;
  - `session.py` — фабрика сессий `get_db()`, создание движка `create_db_engine()` с поддержкой `PRAGMA foreign_keys = ON` для SQLite;
  - `models.py` — SQLAlchemy-модели сущностей со связями владения (`users`, `sessions`, `datasets`, `chats`, `messages`, `results`, `dashboards`);
  - `repositories.py` — сохранение и чтение вызовов инструментов (`ToolResult` со статусами `ok`, `error`, `empty`), потоковый сброс больших табличных данных в Parquet через DuckDB по ссылке `data_ref`, CRUD-операции сущностей;
- `auth/` — Argon2id, постоянные сессии, общий `get_current_user` для Bearer, ограничения попыток/тела и запрет кэширования ответов AUTH-2;
- `migrations/` — среда и версионированные миграции Alembic. Применение: `uv run --locked alembic upgrade head`. `downgrade base` удаляет всё хранилище и применяется только к временной тестовой БД.

## Регистрация и сессии

Регистрация, вход, текущий пользователь и выход реализованы по
[AUTH-2 / #102](../../../docs/decisions/102-auth-api.md): `/api/auth/register`,
`/login`, `/me`, `/logout`. Передача сессии — только `Authorization: Bearer …`,
срок 24 часа, logout отзывает текущий токен. Нужны миграции `alembic upgrade head`.
Все ресурсные API получают владельца из проверенной сессии: анонимный запрос —
401, чужой и неизвестный ID — 404. Открытые SSE повторно проверяют сессию,
включая периоды без событий. Подробности —
[AUTH-3 / #103](../../../docs/decisions/103-owner-isolation.md).

## Реестр результатов и Data API

API предоставляет раздельный доступ к метаданным вычислений и табличным строкам:
- `GET /api/results/{result_id}` — возвращает метаданные `ToolResult` (статус, инструмент `tool`, параметры `params`, SQL-запрос `sql`, ссылку на родительский шаг `source_result_id`, факты `facts`, подсказку графика `chart_hint`) без загрузки строк данных (`data: null`), что ускоряет отображение карточек расчёта и происхождение вычислений;
- `GET /api/results/{result_id}/data` — возвращает табличные данные `ToolResultData` (`columns`, `rows`, `row_count`, `truncated`). Небольшие результаты читаются из SQLite `data_preview`, большие выборки — из Parquet-хранилища (`RESULTS_DATA_DIR`) через DuckDB по ссылке `data_ref = "parquet://<result_id>.parquet"`. При сбое дискового ввода-вывода или повреждении Parquet выполняется безопасный откат к превью с явным флагом `truncated=True`.

Контроль доступа:
- Доступ разрешён только владельцу результата из проверенной Bearer-сессии; связи с чатом, датасетом и вся цепочка `source_result_id` проверяются перед чтением файла.
- Запрос несуществующего, чужого или не имеющего владельца (`owner_id=None`) результата возвращает унифицированный ответ `404 Not Found` по контракту `ErrorResponse`, предотвращая раскрытие данных и сканирование ID.
- `CP1_LOCAL_MODE` не влияет на обязательность сессии. Ответы с личными данными и ошибки ресурсных API имеют `Cache-Control: no-store`.

## Настройки модели

#209: `model_settings.py`, `api/routes/settings.py`. Все эндпоинты требуют сессию и отвечают с `Cache-Control: no-store`.

- `GET /api/settings/model` → `ModelSettingsRead`: действующие адрес и модель, `allowed_models`, `api_key_set`, `api_key_from_ui`, `editable_connection` (`MODEL_SETTINGS_FROM_UI`) и `matches_env`. Ключ не возвращается.
- `PUT /api/settings/model` (`ModelSettingsUpdate`) меняет модель. Ключ и адрес он принимает только при `MODEL_SETTINGS_FROM_UI=true`, иначе отвечает 403. Модель вне `MODEL_ALLOWED_NAMES` при выключенном флаге — 422. Пустая строка возвращает значение из `.env`. Ключ хранится в таблице `model_settings` зашифрованным `SOURCE_ENCRYPTION_KEY`; без него — 409.
- `POST /api/settings/model/check` — один запрос к модели (`max_tokens=1`, 10 с, без повторов). Возвращает `ModelCheckResult` с фиксированным сообщением.

`get_agent_runtime()` читает настройки на каждый ход. Для пары «адрес + модель», отличной от `.env`, выключаются reasoning, тариф и `ALLOW_DATA_SHARING`. Сценарии и рекомендации — в [infra/README.md](../../../infra/README.md#настройка-модели).

## Подключения PostgreSQL

`POST /api/datasets/connect/check` (`SourceCheckRequest`: `kind`, `dsn`) подключается только для чтения и возвращает адрес без пароля и таблицы источника; ничего не сохраняет, требует сессию и `SOURCE_ENCRYPTION_KEY`, ошибки сведены к безопасным категориям (#84).

`POST /api/datasets/connect` принимает `ConnectRequest`: `kind="postgres"` или `"clickhouse"`,
секретный `dsn`, необязательный список `tables` (`public.orders` и т.п.). Ответ
202 возвращает ID обработки; статус, каталог и SSE используются те же, что у CSV.
DSN шифруется серверным `SOURCE_ENCRYPTION_KEY` и связывается с владельцем/датасетом.
Без корректного ключа подключение недоступно (503), CSV продолжает работать.
`DatasetService.get_context(owner_id, dataset_id).open_connection()` проверяет
файл и повторно подключает PostgreSQL только для чтения; данные остаются в БД.
Для PostgreSQL это отдельный DuckDB instance в памяти с read-only ATTACH файла
датасета: параллельные контексты не разделяют `pg_src` и секрет подключения.
ClickHouse копируется в файл датасета при сборке, поэтому его контекст открывается
как у CSV — read-only, без секрета и без обращений к источнику ([#61](../../../docs/decisions/61-clickhouse.md)).
Пороги объёма — `CLICKHOUSE_MAX_ROWS`, `CLICKHOUSE_MAX_BYTES`, `CLICKHOUSE_QUERY_TIMEOUT_SECONDS`.

БД приложения по умолчанию — SQLite (`sqlite:///./var/app.db`); пользователи и сессии используют её вместе с метаданными. DuckDB используется отдельно для аналитики. Сравнение СУБД, контракт сессий, миграции и условия перехода на PostgreSQL описаны в [решении AUTH-1](../../../docs/decisions/101-auth-storage.md).

Загрузка и профилирование вызываются через интерфейс `data/`, расчёты — через `analytics/`, работа агента — через `agent/`. Общие модели берутся из `contracts/`. Для генерации TypeScript-типов frontend схема экспортируется через `python scripts/export_schema.py`.
