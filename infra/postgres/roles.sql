-- Демо-PostgreSQL (#100, ранняя PG-часть): база djedai_demo и две роли.
--   djedai_loader — владелец таблиц, только для загрузки демо-данных (scripts/seed_demo_postgres.py);
--   djedai_reader — учётная запись для подключения в DjedAI: только чтение.
-- Пароли передаются переменными psql и в файл не записываются:
--   psql -U postgres -v loader_password=... -v reader_password=... -f infra/postgres/roles.sql

CREATE ROLE djedai_loader LOGIN PASSWORD :'loader_password';
CREATE ROLE djedai_reader LOGIN PASSWORD :'reader_password';
-- даже без READ_ONLY на стороне клиента транзакции читателя начинаются только на чтение
ALTER ROLE djedai_reader SET default_transaction_read_only = on;

CREATE DATABASE djedai_demo OWNER djedai_loader;
REVOKE ALL ON DATABASE djedai_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE djedai_demo TO djedai_loader, djedai_reader;

\connect djedai_demo

-- с PostgreSQL 15 PUBLIC не может создавать объекты в public; для старых версий — явно
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO djedai_loader;
GRANT USAGE ON SCHEMA public TO djedai_reader;
-- таблицы, которые loader создаст позже, сразу доступны читателю на SELECT
ALTER DEFAULT PRIVILEGES FOR ROLE djedai_loader IN SCHEMA public
    GRANT SELECT ON TABLES TO djedai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO djedai_reader;
