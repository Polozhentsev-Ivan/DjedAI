# Демо-PostgreSQL

Ранняя PG-часть #100: база `djedai_demo` с синтетическим датасетом и read-only учётной записью для проверки подключения PostgreSQL (B1-8 / #60). Контейнерный demo-профиль появится вместе с Compose; до него сервер ставится локально.

## Установка на Fedora

```sh
sudo dnf install postgresql-server postgresql
sudo postgresql-setup --initdb
sudo systemctl start postgresql
```

По умолчанию локальные TCP-подключения в Fedora проверяются через `ident`. Для входа по паролю в `/var/lib/pgsql/data/pg_hba.conf` строки `host ... 127.0.0.1/32` и `::1/128` переводим на `scram-sha-256`, затем `sudo systemctl reload postgresql`.

## Роли и данные

```sh
# роли djedai_loader (загрузка) и djedai_reader (только чтение), база djedai_demo
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -v loader_password='<пароль загрузчика>' -v reader_password='<пароль читателя>' \
  -f infra/postgres/roles.sql

# синтетика из tests/fixtures/synthetic; повторный запуск пересоздаёт таблицы
uv run python scripts/seed_demo_postgres.py \
  --dsn 'postgresql://djedai_loader:<пароль загрузчика>@127.0.0.1:5432/djedai_demo'
```

`roles.sql` читается из корня репозитория (`sudo -u postgres` должен иметь доступ к файлу; иначе передайте его через stdin: `... psql ... < infra/postgres/roles.sql`).

`djedai_reader` получает только `CONNECT`, `USAGE` на `public` и `SELECT` на таблицы (в том числе созданные позже), а `default_transaction_read_only = on` задан самой роли. Именно эта учётная запись подключается в DjedAI.

## Проверка адаптера

```sh
DJEDAI_TEST_PG_DSN='postgresql://djedai_reader:<пароль читателя>@127.0.0.1:5432/djedai_demo' \
DJEDAI_DEMO_PG_LOADER_DSN='postgresql://djedai_loader:<пароль загрузчика>@127.0.0.1:5432/djedai_demo' \
  uv run pytest --strict-markers tests/integration/test_postgres_adapter.py
```

Без `DJEDAI_TEST_PG_DSN` эти тесты пропускаются. `DJEDAI_DEMO_PG_LOADER_DSN` нужен только двум тестам, которые меняют таблицу «снаружи» во время профилирования (временная таблица `live`); без него пропускаются только они. Пароли и DSN в Git не добавляются.
