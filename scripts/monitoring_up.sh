#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for the monitoring stack" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "the Docker Compose plugin is required (docker compose)" >&2
  exit 1
fi

mkdir -p runs
docker compose -f docker-compose.monitoring.yml up -d

echo "Grafana:    http://127.0.0.1:${GRAFANA_PORT:-3000}"
echo "Prometheus: http://127.0.0.1:${PROMETHEUS_PORT:-9090}"
echo "Exporter:   http://127.0.0.1:${EXPORTER_PORT:-9108}/metrics"
echo "Runtime:    ${POLYMARKET_RUN_NAME:-auto} (auto selects highest paper_v* run)"
echo "Default Grafana login: ${GRAFANA_ADMIN_USER:-admin} / ${GRAFANA_ADMIN_PASSWORD:-polymarket-paper}"
