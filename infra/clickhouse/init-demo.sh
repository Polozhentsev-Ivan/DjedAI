#!/bin/sh
# Первый старт тома demo-clickhouse (#100): роли и база из roles.sql, пароли — из окружения Compose.
set -eu
: "${DEMO_CH_LOADER_PASSWORD:?задайте DEMO_CH_LOADER_PASSWORD в .env}"
: "${DEMO_CH_READER_PASSWORD:?задайте DEMO_CH_READER_PASSWORD в .env}"
sha256() { printf '%s' "$1" | sha256sum | cut -d ' ' -f 1; }
sed -e "s/{loader_sha256}/$(sha256 "$DEMO_CH_LOADER_PASSWORD")/" \
    -e "s/{reader_sha256}/$(sha256 "$DEMO_CH_READER_PASSWORD")/" /djedai-demo/roles.sql |
  clickhouse-client --multiquery --host 127.0.0.1 \
    --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD"
