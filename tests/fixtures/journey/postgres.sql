-- Disposable acceptance database only. No host port is published.
CREATE ROLE journey_reader LOGIN PASSWORD 'journey-test-only';
ALTER ROLE journey_reader SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO journey_reader;
CREATE TABLE order_rows (id bigint, ordered_at date, category text, region text, amount bigint);
CREATE TABLE targets (region text, target bigint);
COPY order_rows FROM '/fixtures/orders.csv' WITH (FORMAT csv, HEADER true);
COPY targets FROM '/fixtures/targets.csv' WITH (FORMAT csv, HEADER true);
-- A bad upstream value can break this view without changing its catalog schema.
CREATE VIEW orders AS
SELECT id, ordered_at, category, region,
       CAST(CASE WHEN amount >= 0 THEN amount::text ELSE 'invalid' END AS bigint) AS amount
FROM order_rows;
GRANT SELECT ON orders, order_rows, targets TO journey_reader;
