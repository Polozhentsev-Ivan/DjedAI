-- Disposable acceptance database only. No host port is published.
CREATE ROLE journey_reader LOGIN PASSWORD 'journey-test-only';
ALTER ROLE journey_reader SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO journey_reader;
CREATE TABLE orders (id bigint, ordered_at date, category text, region text, amount bigint);
CREATE TABLE targets (region text, target bigint);
COPY orders FROM '/fixtures/orders.csv' WITH (FORMAT csv, HEADER true);
COPY targets FROM '/fixtures/targets.csv' WITH (FORMAT csv, HEADER true);
GRANT SELECT ON orders, targets TO journey_reader;
