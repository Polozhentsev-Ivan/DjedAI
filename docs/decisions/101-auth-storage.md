# Пользователи, сессии и выбор БД — AUTH-1 / #101

Дата: 20 октября 2026. Решение реализации #101; HTTP-интеграция остаётся в AUTH-2.

## Выбор для текущего проекта

БД приложения — **SQLite**, `APP_DATABASE_URL=sqlite:///./var/app.db`, через SQLAlchemy
и Alembic. В #93 уже созданы `users`, `datasets`, `chats`, `messages`, `results`,
`dashboards` и FK владельцев. DuckDB обслуживает CSV, профилирование и аналитику;
большие результаты хранятся в Parquet. Это разные задачи хранения.

В #101 оставляем пользователей и сессии в общей SQLite локального MVP: сохраняются
транзакции и FK без дополнительного сервера. Для нескольких экземпляров backend
рекомендуем PostgreSQL. Предложение PostgreSQL в плане не означает, что он уже настроен.

| Вариант | Плюсы для #101 | Минусы в проекте | Вывод |
| --- | --- | --- | --- |
| SQLite | Уже внедрена; транзакции, уникальность, FK; без сервера | Один writer на файл; запись может ждать; общий файл между хостами не подходит | Оставить для локального MVP |
| PostgreSQL | Конкурентные транзакции, FK; несколько backend используют один сервер; возможен RLS | Нужны сервис, драйвер, резервирование и проверка переноса | Выбрать при серверном масштабировании |
| DuckDB | Уже установлен; удобен для CSV/агрегатов | Аналитический профиль; в текущем embedded native-file режиме один пишущий процесс; нет `ON DELETE CASCADE` | Оставить для аналитики |
| MongoDB | Гибкие документы; есть транзакции | Переделка реляционной модели; multi-document transactions требуют replica set/sharded cluster | Нет преимуществ для #101 |
| Redis | Быстрый поиск и TTL | Ещё один сервис и настройка persistence; не заменяет FK владельцев | Возможен позже для кэша/лимитов |
| ClickHouse | Аналитика больших объёмов | MergeTree primary key не обеспечивает уникальность; другой профиль обновлений | Источник аналитических данных |

Официальные источники: [SQLite](https://www.sqlite.org/whentouse.html),
[SQLite WAL](https://www.sqlite.org/wal.html),
[PostgreSQL constraints](https://www.postgresql.org/docs/current/ddl-constraints.html),
[PostgreSQL concurrency](https://www.postgresql.org/docs/current/mvcc.html),
[DuckDB concurrency](https://duckdb.org/docs/current/connect/concurrency),
[DuckDB CREATE TABLE](https://duckdb.org/docs/current/sql/statements/create_table),
[MongoDB transactions](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/),
[Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/),
[ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree).
DuckDB также описывает Quack (beta) и DuckLake; проект их не использует.

## Пользователь и пароль

Идентификатор входа — существующий уникальный `username`, с учётом регистра:
`alice` и `Alice` — разные логины. `create_user` принимает 3–50 символов, исключает
логин только из пробелов; пароль не обрезается и не нормализуется. Минимум 6
символов соответствует текущему `UserCreate`, максимум 1024 ограничивает размер
входа. Политика регистрации и ограничение частоты запросов относятся к AUTH-2.

Новый пользователь получает UUID, `is_active`, `created_at`, `updated_at` в UTC.
Пароль хешируется Argon2id через `argon2-cffi==25.1.0`: 64 MiB, 3 итерации,
4 потока, случайная соль 16 байт, хеш 32 байта. В `password_hash` сохраняется
PHC-строка с солью и параметрами. `UserRead` не содержит пароль или хеш.
[argon2-cffi API](https://argon2-cffi.readthedocs.io/en/stable/api.html),
[OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Старые `users.password_hash=NULL` сохраняются как владельцы ресурсов, созданные
до AUTH. Они не проходят парольный вход и не получают сессий. Внутренний
`get_or_create_user` остаётся для таких владельцев; для регистрации используют
`auth.storage.create_user`, который сам хеширует пароль. Неподдерживаемый или
повреждённый хеш означает отказ во входе.

## Сессии и интеграция

`sessions`: `token_hash` (PK), `user_id` (FK в `users`, `ON DELETE CASCADE`),
`created_at`, `expires_at`, `revoked_at`. Есть индексы `user_id`/`expires_at`,
CHECK длины хеша и положительного срока жизни.

`create_session` генерирует `secrets.token_urlsafe(32)` — 256 случайных бит — и
однократно возвращает токен. В БД сохраняется только SHA-256: случайный секрет
не требует медленного хеширования паролей. Срок по умолчанию — 24 часа; вызывающий
слой может передать положительный `ttl`. Продления по активности нет.
[OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

`get_session_user` каждый раз проверяет срок, отзыв и активность аккаунта.
Повторная активация разрешает ещё действующие неотозванные сессии.
`revoke_session` атомарно задаёт дату отзыва и допускает повторный вызов.
Удаление пользователя удаляет сессии через FK. Очистка старых строк — будущая
обслуживающая задача; срок проверяется независимо от неё.

Функции записи фиксируют текущую SQLAlchemy-транзакцию, как существующие
репозитории. Для операции используют отдельную `Session`; после ошибки
уникальности вызывающий код делает `rollback`. Чтение не делает commit.

HTTP-роутеры, Bearer-транспорт браузера/SSE и лимиты входа подключены в
[AUTH-2 / #102](102-auth-api.md). JWT и `SESSION_SECRET` для случайных серверных
сессий не нужны; исходные токены нельзя логировать.

## Владение и миграции

FK `datasets/chats/results/dashboards.owner_id → users.id` заданы в #93.
Миграция `b7f3c1d2e901` добавляет сессии поверх `a5c2f58e6aea`, сохраняя записи.
Откат одной AUTH-миграции удаляет только сессии; `downgrade base` удаляет всё.
`init_db()` не заменяет миграции существующей БД.

FK подтверждает существование владельца, но не проверяет доступ текущего
пользователя или совпадение владельцев связанных ресурсов: это AUTH-3 / #103.
Nullable `results.owner_id` сохранён для совместимости с аудитом без пользователя;
AUTH-3 должен закрыть доступ к таким результатам через пользовательские API.

Из корня репозитория:

```powershell
uv sync --locked
uv run --locked alembic upgrade head
uv run --locked pytest --strict-markers tests/unit/backend/test_auth_storage.py
uv run --locked pytest --strict-markers tests/integration/test_auth_migrations.py
```

Тесты используют временную SQLite: миграции пустой БД, unique/FK/CHECK, upgrade
существующего хранилища, откат AUTH без потери владельцев, чтение пользователя,
сессии и четырёх типов ресурсов из нового процесса. Проверяется SQL cascade и
сохранность другого пользователя. Интеграционный файл запускается отдельно в CI.

## Переход на PostgreSQL

Основания: несколько backend на разных хостах, измеренное ожидание записи,
требования к отдельному серверу БД и централизованному резервированию. Появление
второго пользователя само по себе не требует смены SQLite. WAL улучшает
сосуществование чтения и записи, но не добавляет второго writer; сейчас явно не включён.

Перед переходом добавляют pinned драйвер (например, psycopg), сервис и DSN
`postgresql+psycopg://…`, прогоняют все миграции на реальном PostgreSQL. Проверяют
timestamps, JSON, unique/FK/cascade, конкурентные логины/отзыв и перенос записей.
Смена DSN не переносит данные из SQLite. SQLAlchemy уменьшает объём переделок,
но поддержка PostgreSQL в #101 не заявляется проверенной: драйвер и серверный
прогон в эту задачу не входят.
