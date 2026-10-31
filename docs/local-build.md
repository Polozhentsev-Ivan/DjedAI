# Локальная сборка и запуск

Инструкция сборки DjedAI **из исходников через Docker Compose**. Docker собирает Python backend и React frontend, nginx открывает приложение на `http://localhost:8080`. SQLite и файлы данных хранятся в постоянном томе. Для основного сценария на компьютере нужны Git, Docker и браузер; устанавливать Python, uv, Node.js, npm и локальную модель отдельно не требуется.

Сначала пройдите разделы 1–5 с CSV. Свои БД и необязательный демо-профиль описаны дальше. Работа в интерфейсе — в [руководстве пользователя](user-guide.md), все серверные параметры — в [конфигурации](configuration.md), резервирование и обновление — в [эксплуатации](operations.md). Разработка с hot reload — в [development.md](development.md).

## 1. Подготовить окружение

- **Windows:** установите и запустите [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) с Linux-контейнерами; обычный вариант — WSL 2 backend. Команды ниже можно выполнять в PowerShell.
- **macOS:** Docker Desktop. **Linux:** Docker Engine с плагином Compose либо Docker Desktop. Варианты установки — в [документации Docker](https://docs.docker.com/compose/install/).
- Нужны доступ к Git-репозиторию, реестрам образов/PyPI/npm при первой сборке и API выбранной модели во время анализа. PostgreSQL дополнительно использует расширение DuckDB `postgres`, которое может загружаться при первом обращении.

Проверьте из терминала:

```sh
git --version
docker version
docker info --format '{{.OSType}}'
docker compose version
```

У `docker version` должны выводиться Client и Server, тип сервера — `linux`, Compose — v2 с поддержкой `up --wait`. CLI без доступного Docker engine недостаточен. GPU для внешнего API модели не нужен. Требования RAM/диска зависят от данных; проверенного минимального размера машины для всего приложения пока нет.

## 2. Получить исходники и создать конфигурацию

Если репозиторий ещё не скачан:

```sh
git clone https://github.com/Polozhentsev-Ivan/DjedAI.git
cd DjedAI
git rev-parse HEAD
```

Репозиторий приватный: Git должен иметь доступ к вашему GitHub-аккаунту. Если checkout уже есть, перейдите в его корень — там лежат `compose.yaml`, `.env.example`, `src/` и `frontend/`. Это может быть папка `repo/` в рабочем пространстве команды. Сохраните SHA из `git rev-parse HEAD`, чтобы повторить сборку той же версии. Для заранее выбранного commit в чистом checkout выполните `git switch --detach ПОЛНЫЙ_SHA`.

Создайте `.env`, сохранив существующий файл, если он уже есть.

PowerShell:

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

Bash/zsh:

```sh
test -f .env || cp .env.example .env
```

Откройте `.env` в редакторе и настройте модель:

```dotenv
MODEL_BASE_URL=https://api.deepinfra.com/v1/openai
MODEL_NAME=Qwen/Qwen3.6-35B-A3B
MODEL_API_KEY=ваш_ключ_провайдера
MODEL_SETTINGS_FROM_UI=false
ALLOW_DATA_SHARING=false
DJEDAI_PORT=8080
```

Замените `ваш_ключ_провайдера` действительным ключом. `.env` исключён из Git и Docker build context. `MODEL_BASE_URL` — полный базовый URL OpenAI-совместимого API, включая путь провайдера; для другой модели нужны совместимый endpoint и точный ID. Ключи не помещаются в `VITE_*`: эти переменные видны браузеру.

Для первых CSV-проверок остальные настройки можно оставить из `.env.example`. `SOURCE_ENCRYPTION_KEY` пока может быть пустым: он нужен для сохранённых подключений БД и ключа модели из UI, а не для CSV. `SESSION_SECRET` для используемых серверных Bearer-сессий не требуется.

Без ключа провайдера можно открыть UI, зарегистрироваться и загрузить CSV, но ответ модели не получится. В `.env.example` включено `CATALOG_DESCRIPTIONS=true`: при подготовке каталога тоже вызывается модель. Для каталога без этих вызовов задайте `CATALOG_DESCRIPTIONS=false`; типы и статистический профиль останутся доступны.

`ALLOW_DATA_SHARING=false` ограничивает контекст внешней модели, но **не отключает внешний inference**: передаются вопрос, метаданные, факты, агрегаты и разрешённые значения текущего результата. Подробности — в [политике передачи данных](decisions/173-data-transfer-policy.md). Настройка `ALLOWED_OUTBOUND_HOSTS` сама по себе не является сетевым фильтром.

## 3. Собрать и запустить

Команды одинаковы для PowerShell и Bash/zsh, выполняются из корня checkout:

```sh
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

Первая команда проверяет конфигурацию без вывода её значений. Вторая скачивает закреплённые базовые образы, восстанавливает зависимости по lock-файлам и собирает два образа приложения. Первая сборка зависит от сети и может занять несколько минут; `--wait-timeout` относится к ожиданию готовности сервисов. Дождитесь завершения команды. При сетевой ошибке исправьте доступ и повторите её.

Успешный результат: `backend` и `frontend` имеют статус `healthy`. При старте backend сам применяет Alembic-миграции. Отдельный PostgreSQL для метаданных приложения не нужен: используется SQLite в томе.

Откройте `http://localhost:8080`. Проверка через общий nginx/API:

PowerShell:

```powershell
Invoke-RestMethod http://localhost:8080/api/health
```

Bash/zsh:

```sh
curl --fail http://localhost:8080/api/health
```

Ожидается успешный ответ health. Он подтверждает доступность API, но не модель или правильность расчётов — для этого следующий раздел. Если изменили `DJEDAI_PORT`, используйте новый порт во всех URL. Публикуется только `127.0.0.1`; доступ из локальной сети этим Compose не настраивается.

## 4. Проверить первый пользовательский сценарий

1. Откройте приложение, зарегистрируйтесь и войдите. Готового общего логина нет; автоматический bootstrap в Compose выключен.
2. На странице «Источники» загрузите `tests/fixtures/csv/orders_utf8_comma.csv` из checkout. Дождитесь готовности и откройте каталог. В файле **3 строки данных**, не считая заголовка.
3. Создайте чат по этому датасету и спросите: **«Сколько строк в загруженной таблице?»** Ожидаемое число — **3**. Посмотрите сохранённый результат расчёта: число в тексте должно соответствовать строкам результата.
4. Сохраните адрес чата. Обновите страницу по этому адресу: история и результат должны открыться снова.
5. Перезапустите сервисы и повторно откройте тот же чат:

   ```sh
   docker compose restart
   docker compose up -d --wait --wait-timeout 180
   ```

6. Убедитесь, что аккаунт, датасет, каталог, история и сохранённый результат доступны. Если сессия истекла, войдите в тот же аккаунт.

Это минимальная проверка установки. Качество свободных вопросов, графиков и дашбордов подтверждается отдельными [golden/eval-проверками](../eval/README.md). Если модель недоступна или результат неправильный, зафиксируйте ошибку: успешный health не заменяет этот шаг.

## 5. Остановка, повторный запуск и данные

```sh
docker compose down
docker compose up -d --wait --wait-timeout 180
```

Обычные `down`, `stop` и `restart` сохраняют данные. При стандартном имени проекта том называется `djedai_app-data` и содержит `/app/var`: SQLite, исходные CSV, DuckDB/каталоги, Parquet результатов и диагностику. Локальная папка `var/` не является этим Docker-томом. Compose закрепляет контейнерные пути независимо от локальных `APP_DATABASE_URL` и `*_DATA_DIR` в `.env`.

**Не используйте `docker compose down -v`, если данные нужны:** эта команда удаляет тома, включая данные приложения. Смена имени Compose-проекта создаст другие тома и может выглядеть как исчезновение данных. Для независимой установки можно использовать `docker compose -p другое_имя …`, но тогда указывайте то же имя во всех командах и выбирайте свободный порт.

После изменения `.env` пересоздайте контейнеры; один `restart` не перечитывает окружение:

```sh
docker compose up -d --force-recreate --wait --wait-timeout 180
```

После изменения исходников повторите команду с `--build`. Для обновления на другой commit сначала сохраните `.env`, ключ шифрования и согласованную копию всего тома при остановленном backend: новая версия применяет миграции при старте. Возврат к старому образу без восстановления совместимых данных не считается проверенным откатом. Автоматическое обновление и восстановление выпуска относятся к отдельной работе [#110](https://github.com/Polozhentsev-Ivan/DjedAI/issues/110).

## 6. Настройки модели и подключение своих БД

По умолчанию `MODEL_SETTINGS_FROM_UI=false`: ключ и адрес берутся из `.env`, а в UI можно выбирать модель из `MODEL_ALLOWED_NAMES` (исходный `MODEL_NAME` разрешён всегда). Действующие настройки модели общие для установки, а не личные для каждого пользователя.

Для локального запуска одним пользователем можно включить `MODEL_SETTINGS_FROM_UI=true`. Тогда адрес, модель и ключ задаются в разделе «Настройки»; сохранённые значения имеют приоритет над `.env`. Для шифрования ключа сначала нужен `SOURCE_ENCRYPTION_KEY`. Если установкой пользуются другие люди, оставьте флаг выключенным. Особенности переключения моделей — в [infra/README.md](../infra/README.md#настройка-модели).

Чтобы подключить PostgreSQL/ClickHouse или хранить ключ модели из интерфейса, сгенерируйте Fernet key в уже запущенном backend:

```sh
docker compose exec backend uv run --locked --no-sync python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Скопируйте строку целиком в `SOURCE_ENCRYPTION_KEY` своего `.env`, сохраните ключ для восстановления и примените конфигурацию командой `up --force-recreate` из раздела 5. Генерируйте его один раз для установки: смена ключа лишит доступа к прежним зашифрованным подключениям и ключу модели из UI.

На странице «Источники» выберите PostgreSQL либо ClickHouse, укажите read-only подключение и таблицы. Адрес должен быть доступен **из backend-контейнера**:

| Где работает БД | Адрес из backend |
| --- | --- |
| Демо-сервис этого Compose | `demo-postgres:5432` или `demo-clickhouse:8123`. |
| На компьютере с Docker Desktop | `host.docker.internal` и порт хоста. `localhost` внутри контейнера обозначает сам контейнер. |
| На отдельной машине | Доступные backend DNS/IP и порт БД. |

В Docker Engine на Linux имя `host.docker.internal` может требовать отдельной настройки; для первого запуска проще использовать демо-профиль ниже. PostgreSQL использует DSN `postgresql://user:password@host:5432/database`; ClickHouse — HTTP URL `http://user:password@host:8123/database`. Спецсимволы пароля в URL нужно percent-encode. Подключайте роль с правами чтения. PostgreSQL читается как внешний источник; ClickHouse копирует выбранные таблицы в ограниченный локальный снимок ([решение #61](decisions/61-clickhouse.md)).

## 7. Необязательно: демо-PostgreSQL и ClickHouse

Профиль нужен для проверки подключения к БД; обычная сборка с CSV от него не зависит. Здесь используются только контейнерные Python/uv. Перед началом настройте `SOURCE_ENCRYPTION_KEY` по разделу 6.

В `.env` задайте шесть непустых паролей: `DEMO_PG_ADMIN_PASSWORD`, `DEMO_PG_LOADER_PASSWORD`, `DEMO_PG_READER_PASSWORD`, `DEMO_CH_ADMIN_PASSWORD`, `DEMO_CH_LOADER_PASSWORD`, `DEMO_CH_READER_PASSWORD`. Для генерации каждого пароля можно использовать:

```sh
docker compose exec backend uv run --locked --no-sync python -c "import secrets; print(secrets.token_hex(24))"
```

Для команд заполнения **в контейнере** добавьте в `.env` адреса ниже. Ссылки `${…}` оставьте буквально: Compose подставит пароли из этого файла. Генерируемые выше hex-пароли не требуют URL-кодирования.

```dotenv
DJEDAI_DEMO_PG_LOADER_DSN=postgresql://djedai_loader:${DEMO_PG_LOADER_PASSWORD}@demo-postgres:5432/djedai_demo
DJEDAI_DEMO_PG_READER_DSN=postgresql://djedai_reader:${DEMO_PG_READER_PASSWORD}@demo-postgres:5432/djedai_demo
DJEDAI_DEMO_CH_LOADER_URL=http://djedai_loader:${DEMO_CH_LOADER_PASSWORD}@demo-clickhouse:8123
DJEDAI_DEMO_CH_READER_URL=http://djedai_reader:${DEMO_CH_READER_PASSWORD}@demo-clickhouse:8123
```

Поднимите БД:

```sh
docker compose --profile demo up -d --wait --wait-timeout 180 demo-postgres demo-clickhouse
```

Роли и пароли создаются только на пустых томах при первом старте. Изменение пароля в `.env` не меняет пароль уже созданной роли. **Seed-команды пересоздают демо-таблицы:** используйте их для демо-БД, не для собственных данных.

Из корня checkout выполните три команды. Они одинаковы в PowerShell и Bash/zsh; `${PWD}` — текущая папка. Скрипты, CSV и эталоны монтируются только для этих одноразовых контейнеров, а не включаются в образы приложения:

```sh
docker compose --profile demo run --rm --no-deps --volume "${PWD}/scripts:/app/scripts:ro" --volume "${PWD}/tests/fixtures/synthetic:/app/tests/fixtures/synthetic:ro" --volume "${PWD}/eval:/app/eval:ro" --entrypoint uv backend run --locked --no-sync python /app/scripts/seed_demo_postgres.py
docker compose --profile demo run --rm --no-deps --volume "${PWD}/scripts:/app/scripts:ro" --volume "${PWD}/tests/fixtures/synthetic:/app/tests/fixtures/synthetic:ro" --volume "${PWD}/eval:/app/eval:ro" --entrypoint uv backend run --locked --no-sync python /app/scripts/seed_demo_clickhouse.py
docker compose --profile demo run --rm --no-deps --volume "${PWD}/scripts:/app/scripts:ro" --volume "${PWD}/tests/fixtures/synthetic:/app/tests/fixtures/synthetic:ro" --volume "${PWD}/eval:/app/eval:ro" --entrypoint uv backend run --locked --no-sync python /app/scripts/verify_demo_db.py
```

Ожидается `status: passed`; `sales` содержит 4 376 строк, сумма `total_amount` — 1 268 254 502,40 RUB. Проверяются таблицы и агрегаты в обеих БД, а также отказ читателю в записи. Остальные эталоны — в [infra/README.md](../infra/README.md#контрольные-значения).

В UI подключите роль `djedai_reader` с соответствующим паролем: PostgreSQL — `postgresql://djedai_reader:ПАРОЛЬ@demo-postgres:5432/djedai_demo`, ClickHouse — `http://djedai_reader:ПАРОЛЬ@demo-clickhouse:8123/djedai_demo`. Порты хоста 55432/58123 нужны для клиентов на компьютере, а из backend используются контейнерные 5432/8123.

Запустить приложение вместе с демо-БД:

```sh
docker compose --profile demo up -d --wait --wait-timeout 180
```

Остановить весь профиль с сохранением томов:

```sh
docker compose --profile demo down
```

## 8. Если запуск не удался

Сначала соберите состояние и последние сообщения:

```sh
docker compose ps -a
docker compose logs --tail 100 backend frontend
```

Для демо-БД добавьте `--profile demo` и имена `demo-postgres demo-clickhouse`. В диагностике не публикуйте `.env`, API keys и полные DSN. Используйте `config --quiet`, а не печать всей конфигурации с паролями.

| Симптом | Что проверить |
| --- | --- |
| Cannot connect to Docker daemon / нет Server | Запущен ли Docker Desktop/engine, выбран ли правильный context и Linux-контейнеры. |
| Нет `compose` или `--wait` | Установлен ли актуальный Compose v2, а не старый `docker-compose`. |
| Нет `compose.yaml` / `.env` | Терминал должен находиться в корне checkout; файл должен называться `.env`, без суффикса `.txt`. |
| Ошибка скачивания образа/зависимости | Доступ к GitHub Container Registry, Docker Hub, PyPI/npm, proxy и свободное место Docker. Повторите сборку после исправления сети. |
| Порт 8080 занят | Измените `DJEDAI_PORT`, например на 8081, и повторите `up`; открывайте новый URL. |
| Backend unhealthy / ошибка Alembic | Backend logs, диск, права и совместимость данных. Не удаляйте том как способ исправить миграцию. |
| UI работает, модель отвечает ошибкой | Согласованы ли адрес, имя и ключ, доступен ли провайдер из контейнера. Изменение `.env` требует `up --force-recreate`; сохранённые UI-настройки могут иметь приоритет. |
| Новая конфигурация не применяется | `restart` не обновляет env. Пересоздайте контейнеры, проверьте приоритет настроек из раздела 6. |
| БД недоступна / ключ не расшифровывается | Адрес из контейнера, read-only роль, `SOURCE_ENCRYPTION_KEY`; для прежних зашифрованных данных нужен прежний ключ. |
| После запуска «пропали» данные | Тот ли аккаунт, Docker context и Compose project; не создан ли другой том из-за `-p`. Не запускайте удаление томов. |
| В браузере 401 | Войдите заново в тот же аккаунт. API/SSE используют Bearer-сессию; `CP1_LOCAL_MODE` не отключает авторизацию. |

При обращении за помощью укажите ОС, версии Docker/Compose, SHA исходников, команду, этап сбоя и безопасный фрагмент лога. [Диагностика агента](agent-diagnostics.md) помогает отличить ошибку источника, инструмента и модели.
