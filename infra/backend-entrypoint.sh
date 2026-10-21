#!/bin/sh
set -eu

uv run --locked --no-sync alembic upgrade head
uv run --locked --no-sync python -m djedai.backend.bootstrap_local_user
exec uv run --locked --no-sync uvicorn djedai.backend.app:app --host 0.0.0.0 --port 8000
