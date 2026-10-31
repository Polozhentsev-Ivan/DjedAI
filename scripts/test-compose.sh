#!/usr/bin/env bash
# Real production images, nginx, browser smoke and persistence, in a disposable project.
set -euo pipefail
cd "$(dirname "$0")/.."

output=${1:?Usage: bash scripts/test-compose.sh /absolute/evidence/directory}
mkdir -p "$output"
output=$(cd "$output" && pwd)
# Always override inherited production project/port/env settings.
export COMPOSE_PROJECT_NAME="djedai-ci-${GITHUB_RUN_ID:-local-$(date +%s)-$$}-${GITHUB_RUN_ATTEMPT:-1}"
export DJEDAI_PORT=0
export DJEDAI_ENV_FILE="$PWD/.env.example"
export COMPOSE_PROFILES=
export DJEDAI_COMPOSE_ACCEPTANCE=local
if [[ ${GITHUB_ACTIONS:-} == true ]]; then
    export DJEDAI_COMPOSE_ACCEPTANCE=true
fi
export DJEDAI_SMOKE_USERNAME="ci-smoke-${GITHUB_RUN_ID:-local}"
export DJEDAI_SMOKE_PASSWORD
DJEDAI_SMOKE_PASSWORD=$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))')
if [[ ${GITHUB_ACTIONS:-} == true ]]; then
    echo "::add-mask::$DJEDAI_SMOKE_PASSWORD"
fi
compose=(docker compose --env-file "$DJEDAI_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f infra/compose.test.yaml)
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

frontend_url() {
    local address
    address=$("${compose[@]}" port frontend 80)
    [[ $address =~ ^127\.0\.0\.1:([0-9]+)$ ]] || { echo "Unexpected frontend binding: $address" >&2; return 1; }
    printf 'http://%s' "$address"
}
"${compose[@]}" up --build -d --wait --wait-timeout 180
url=$(frontend_url)
printf 'project=%s\nurl=%s\n' "$COMPOSE_PROJECT_NAME" "$url" | tee "$output/compose-run.txt"
npm --prefix frontend run smoke:cp1 -- --url "$url" --output "$output"
"${compose[@]}" restart
"${compose[@]}" up -d --wait --wait-timeout 180
# Docker may allocate a different host port when a container restarts.
url=$(frontend_url)
printf 'restart_url=%s\n' "$url" | tee -a "$output/compose-run.txt"
curl --fail "$url/api/health"
npm --prefix frontend run smoke:cp1 -- --url "$url" --output "$output" --verify
