#!/usr/bin/env bash
# Manual production browser acceptance. Does not start the CI runner.
set -euo pipefail
cd "$(dirname "$0")/.."
output=${1:?Usage: bash scripts/test-journey.sh /absolute/evidence/directory}
mkdir -p "$output"
output=$(cd "$output" && pwd)
[[ ! -e "$output/journey-report.json" ]] || { echo 'Use a new evidence directory' >&2; exit 1; }
export COMPOSE_PROJECT_NAME="djedai-journey-$(date +%s)-$$"
export DJEDAI_PORT=0 DJEDAI_ENV_FILE="$PWD/.env.example" COMPOSE_PROFILES= DJEDAI_COMPOSE_ACCEPTANCE=local
export JOURNEY_PASSWORD JOURNEY_ENCRYPTION_KEY
JOURNEY_PASSWORD=$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))')
JOURNEY_ENCRYPTION_KEY=$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("base64").replaceAll("+", "-").replaceAll("/", "_"))')
compose=(docker compose --env-file "$DJEDAI_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f infra/compose.test.yaml -f infra/compose.journey.yaml)
cleanup() {
    local status=$?
    trap - EXIT
    "${compose[@]}" logs --no-color > "$output/compose.log" 2>&1 || true
    "${compose[@]}" down --volumes --remove-orphans || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
url_for() {
    local address
    address=$("${compose[@]}" port "$1" "$2")
    [[ $address =~ ^127\.0\.0\.1:([0-9]+)$ ]] || { echo "Unexpected binding: $address" >&2; return 1; }
    printf 'http://%s' "$address"
}
run_browser() {
    node frontend/scripts/journey.mjs --url "$(url_for frontend 80)" --model-url "$(url_for model 8000)" --output "$output" --phase "$1"
}
git rev-parse HEAD > "$output/application-sha.txt"
git status --porcelain > "$output/worktree-status.txt"
"${compose[@]}" up --build -d --wait --wait-timeout 180
printf 'project=%s\n' "$COMPOSE_PROJECT_NAME" > "$output/compose-run.txt"
run_browser prepare
# Change the live source, then restart with the same app-data and source state.
"${compose[@]}" exec -T journey-postgres psql -U postgres -d journey -v ON_ERROR_STOP=1 -c 'UPDATE orders SET amount = amount + 10 WHERE id = 1;'
"${compose[@]}" restart
"${compose[@]}" up -d --wait --wait-timeout 180
run_browser verify
# Genuine source failure of one recipe; the independent targets recipe must still work.
"${compose[@]}" exec -T journey-postgres psql -U postgres -d journey -v ON_ERROR_STOP=1 -c 'ALTER TABLE orders RENAME COLUMN amount TO unavailable_amount;'
run_browser partial
