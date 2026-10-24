-- Демо-ClickHouse (#100): база djedai_demo и две роли, по аналогии с infra/postgres/roles.sql.
--   djedai_loader — только загрузка демо-данных (scripts/seed_demo_clickhouse.py);
--   djedai_reader — учётная запись для подключения в DjedAI: только чтение.
-- {loader_sha256}/{reader_sha256} — SHA-256 паролей (hex); подставляет init-demo.sh,
-- сами пароли в файл и в SQL не попадают.

CREATE DATABASE IF NOT EXISTS djedai_demo;

CREATE USER IF NOT EXISTS djedai_loader
    IDENTIFIED WITH sha256_hash BY '{loader_sha256}' DEFAULT DATABASE djedai_demo;
GRANT ALL ON djedai_demo.* TO djedai_loader;

-- readonly = 2: запись и DDL запрещены, но адаптер может задавать лимиты запроса
-- (max_execution_time, max_result_rows, overflow_mode — решение #47 §7); CONST не даёт снять сам readonly
CREATE USER IF NOT EXISTS djedai_reader
    IDENTIFIED WITH sha256_hash BY '{reader_sha256}' DEFAULT DATABASE djedai_demo
    SETTINGS readonly = 2 CONST;
GRANT SELECT ON djedai_demo.* TO djedai_reader;
