# Разработка

Глава описывает работу с исходниками. Если нужно только запустить приложение, используйте [Compose-инструкцию](../administrator/local-build.md): она не требует отдельной установки Python/Node на host. Устройство сервисов и хранения описано в [backend](backend.md), браузерного приложения — в [frontend](frontend.md).

## Окружение

| Инструмент | Где закреплена версия |
| --- | --- |
| Python | [.python-version](../../.python-version), `requires-python` в [pyproject.toml](../../pyproject.toml) |
| Python dependencies | `pyproject.toml` + [uv.lock](../../uv.lock) |
| uv для CI/образа | [workflow](../../.github/workflows/ci.yml), [backend Dockerfile](../../infra/backend.Dockerfile) |
| Node | [.nvmrc](../../.nvmrc) и `engines` frontend |
| npm и frontend dependencies | [package.json](../../frontend/package.json), [package-lock.json](../../frontend/package-lock.json) |

Версии берутся из checkout, а не из глобального окружения соседнего проекта. Python использует src-layout и `tool.uv.package=false`; standalone runtime нужен `PYTHONPATH=src`. Pytest получает этот путь из конфигурации.

## Подготовка

Из корня репозитория:

```sh
uv sync --locked
```

Создайте локальный `.env` по [инструкции](../administrator/local-build.md), сохранив существующие значения. Настройки и секреты не помещаются в Git. Отдельная dev-база и каталоги данных помогают не смешать тестовые действия с рабочей установкой.

```sh
uv run --locked alembic upgrade head
```

Alembic добавляет/меняет схему существующей БД. `create_all` не заменяет миграции. Применение к рабочей базе выполняется по [эксплуатационной процедуре](../administrator/operations.md).

## Запуск backend

PowerShell:

```powershell
$env:PYTHONPATH = 'src'
uv run --locked uvicorn djedai.backend.app:app --reload --host 127.0.0.1 --port 8000
```

Bash/zsh:

```sh
PYTHONPATH=src uv run --locked uvicorn djedai.backend.app:app --reload --host 127.0.0.1 --port 8000
```

API — `http://localhost:8000/api`, health — `/health` или `/api/health`. Все ресурсные операции требуют настоящую сессию. Вход выполняется через UI или API; включение legacy CP1-флага не даёт доступа без Bearer.

## Запуск frontend

Во втором терминале из корня checkout:

```sh
fnm use
cd frontend
npm ci
npm run dev
```

Можно использовать другой менеджер Node, соблюдая `.nvmrc` и `engines`. Dev UI — localhost:5173, `/api` проксируется на backend:8000. Backend CORS для отдельного origin задаётся явно; production Compose работает через один origin.

Для mocks задайте `VITE_API_MOCKS=true` в `frontend/.env.local`. Это средство разработки состояний, не режим приёмки живых источников. Подробности proxy, mocks и UI — [frontend](frontend.md).

## Карта изменений

| Что меняется | Основное место | Что сверить рядом |
| --- | --- | --- |
| CSV/источник/профиль | `src/djedai/data/`, `backend/dataset_service.py` | Catalog, source reader, revision, progress, данные владельца |
| Инструмент/метрика | `src/djedai/analytics/`, `backend/metric_service.py` | Schema registry, scope всех SQL, ToolResult/facts/quality, независимый эталон |
| Поведение агента | `src/djedai/agent/`, `prompts/` | Memory, typed responses, data policy, deadline, непубликация черновика |
| HTTP/хранение | `src/djedai/backend/` | Owner, DB/FS paths, миграции, SSE, отмена и admission |
| UI | `frontend/src/` | Session lifetime, query keys/cache, late responses, error/reload, блоки |
| Общая модель | `src/djedai/contracts/` | Примеры, export/OpenAPI, generated TS, все потребители |
| Сборка и запуск | `compose.yaml`, `infra/`, `.env.example` | Постоянные данные, секреты, миграции, health/proxy и docs |

Общее устройство и направления вызовов — [архитектура](architecture.md). Конкретные public API — [api.md](api.md).

## Куда класть вспомогательные материалы

| Каталог | Назначение |
| --- | --- |
| `prompts/` | Тексты промптов и примеры агента; код вызова модели остаётся в `src/djedai/agent/` |
| `tests/unit/` | Детерминированная логика без реальной модели, GPU и внешней сети |
| `tests/integration/` | Взаимодействие с реальными тестовыми БД, API, SSE и хранилищем |
| `tests/e2e/` | Пользовательские сценарии через несколько компонентов |
| `tests/fixtures/` | Маленькие синтетические примеры с известными ответами, без личных данных и секретов |
| `eval/` | Эталонные вопросы и воспроизводимая оценка агента |
| `infra/` | Dockerfile, конфигурации сервисов и демо-источников |
| `scripts/` | Вспомогательные команды: синтетика, демо-данные, проверки |

## Контракты Python ↔ TypeScript

После намеренного изменения Pydantic/OpenAPI:

```sh
uv run --locked python scripts/export_frontend_contracts.py
npm --prefix frontend run gen:types
```

Проверка актуальности:

```sh
uv run --locked python scripts/export_frontend_contracts.py --check
git diff -- frontend/schema frontend/src/api/generated.ts
```

`generated.ts` вручную не исправляется. Обновление схемы не создаёт отсутствующий handler и не подтверждает wiring интерфейса; проверяются соответствующие потребители.

## Обычные проверки

Из корня:

```sh
node --test scripts/check-repository.test.mjs scripts/check-ci-changes.test.mjs
node scripts/check-repository.mjs
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/export_frontend_contracts.py --check
uv run --locked pytest --strict-markers -m "not integration and not llm" --cov --cov-config=pyproject.toml --cov-report=term-missing --cov-fail-under=80
```

Frontend:

```sh
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run test:coverage
npm --prefix frontend run build
```

Integration/browser/реальные источники и eval запускаются по изменению. Команды и границы — [качество](quality.md). Платный inference не является побочным действием обычного unit suite; реальные model-прогоны выбираются явно.

## Миграции и сохранённые форматы

SQLAlchemy-модели — `backend/db/models.py`, Alembic revisions — `backend/migrations/versions/`. Изменение существующей схемы сопровождается revision и проверкой перехода с предыдущего состояния. Для сохранённых JSON-контрактов учитываются `schema_version` и совместимость читателя.

Один `Session` не разделяется произвольно между потоками. Worker владеет своими ресурсами и освобождает их в `finally`. `check_same_thread=False` не делает Session потокобезопасной. Запись результата и файла имеет отдельные правила ownership/idempotency; их нельзя заменять случайным новым путём `data_ref`.

## Правила изменения поведения

- Server owner, selected scope, provenance и policy context остаются обязательными.
- Новая долгая операция учитывает `current_budget()` и cancel; permit освобождается по фактическому завершению.
- Error/empty не превращаются в успешный ноль; truncated data не используется как полный источник зависимой агрегации.
- Prompt — runtime-вход: его изменение требует соответствующих проверок, даже если файл Markdown.
- Для новых переменных обновляются конфигурация, безопасный example и [справочник](../administrator/configuration.md).
- Документируется результат изменения и его проверка; историческая метрика тестов не переносится на новый SHA.

Процесс issue/PR, роли и CI — [project-workflow.md](project-workflow.md), обязательные соглашения — [CONTRIBUTING.md](../../CONTRIBUTING.md).
