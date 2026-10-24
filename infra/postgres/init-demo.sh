#!/bin/sh
# Первый старт тома demo-postgres (#100): роли и база из roles.sql, пароли — из окружения Compose.
set -eu
: "${DEMO_PG_LOADER_PASSWORD:?задайте DEMO_PG_LOADER_PASSWORD в .env}"
: "${DEMO_PG_READER_PASSWORD:?задайте DEMO_PG_READER_PASSWORD в .env}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v loader_password="$DEMO_PG_LOADER_PASSWORD" -v reader_password="$DEMO_PG_READER_PASSWORD" \
  -f /djedai-demo/roles.sql
