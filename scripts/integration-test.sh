#!/usr/bin/env bash
# Start stirling-pdf + audit-proxy (test override), run integration tests,
# check Stirling is not reachable from outside Docker, then stop the stack.
#   scripts/integration-test.sh          # run and tear down
#   scripts/integration-test.sh --keep   # leave the stack running afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP=0; [[ "${1:-}" == "--keep" ]] && KEEP=1
COMPOSE=(docker compose -f docker-compose.yml -f compose.test.yml)
PORT="${TEST_PROXY_PORT:-18081}"

if [[ ! -f .env ]]; then
  echo "No .env found - using .env.example for the test run"
  # Read KEY=VALUE lines literally, like Compose does. Not `export $(… | xargs)`
  # or `source`: values may contain spaces, e.g. AD_BIND_USER (OU=Service Accounts).
  while IFS='=' read -r key value; do
    export "$key=$value"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env.example)
fi
mkdir -p logs

cleanup() { [[ $KEEP -eq 1 ]] || "${COMPOSE[@]}" down; }
trap cleanup EXIT

echo "==> starting stack"
"${COMPOSE[@]}" up -d --build --wait stirling-pdf audit-proxy

echo "==> Stirling must NOT be published on the host"
bindings=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$("${COMPOSE[@]}" ps -q stirling-pdf)")
if [[ "$bindings" != "{}" && "$bindings" != "null" ]]; then
  echo "FAIL: stirling-pdf has published ports: $bindings"; exit 1
fi
if curl -s -o /dev/null --max-time 3 http://127.0.0.1:8080/; then
  echo "FAIL: something answers on 127.0.0.1:8080"; exit 1
fi
echo "ok: stirling-pdf is not reachable from the host"

echo "==> pytest (integration)"
cd audit-proxy
AUDIT_PROXY_URL="http://127.0.0.1:${PORT}" AUDIT_LOG_DIR="$(cd .. && pwd)/logs" \
  uv run --frozen pytest -m integration -q
