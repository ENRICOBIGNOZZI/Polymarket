#!/usr/bin/env bash
set -u

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
TARGET_SHA="${1:-unknown}"

printf '[mac-deploy] candidate_health_diagnostics_begin\n'
printf '[mac-deploy] candidate_expected_sha=%s\n' "$TARGET_SHA"
printf '[mac-deploy] candidate_actual_sha=%s\n' "$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"

meta="$(python3 - "$APP_DIR/config/live_champion.json" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
m = json.loads(Path(sys.argv[1]).read_text())
print(f"{m.get('version','')}\t{m.get('run_root','')}\t{m.get('config','')}\t{m.get('loop','')}")
PY
)"
IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
printf '[mac-deploy] candidate_champion_version=%s\n' "${version:-unknown}"
printf '[mac-deploy] candidate_run_root=%s\n' "${run_root_rel:-unknown}"

probe_http() {
  local label="$1" url="$2" payload
  payload="$(curl --silent --show-error --max-time 4 --write-out $'\n__HTTP_STATUS__:%{http_code}' "$url" 2>&1 || true)"
  printf '[mac-deploy] %s_begin\n' "$label"
  printf '%s\n' "$payload" | tail -n 40
  printf '[mac-deploy] %s_end\n' "$label"
}

probe_http exporter_healthz http://127.0.0.1:9108/healthz
probe_http prometheus_ready http://127.0.0.1:9090/-/ready
probe_http grafana_health http://127.0.0.1:3000/api/health

metrics="$(curl --silent --show-error --max-time 4 http://127.0.0.1:9108/metrics 2>&1 || true)"
printf '[mac-deploy] candidate_key_metrics_begin\n'
printf '%s\n' "$metrics" | grep -E '^(polymarket_runtime_info|polymarket_runtime_pnl_usd|polymarket_allocator_state_present|polymarket_allocator_models_expected|polymarket_v7_runtime_info|polymarket_v7_local_factor_clusters|polymarket_v7_model_(alive|staleness_seconds|fills_total|gross_exposure_usd|drawdown))' | head -n 120 || true
printf '[mac-deploy] candidate_key_metrics_end\n'

if [[ -x "$APP_DIR/ops/macos_service_control.sh" || -f "$APP_DIR/ops/macos_service_control.sh" ]]; then
  printf '[mac-deploy] candidate_service_status_begin\n'
  bash "$APP_DIR/ops/macos_service_control.sh" status 2>&1 | tail -n 100 || true
  printf '[mac-deploy] candidate_service_status_end\n'
fi

if [[ "${run_root_rel:-}" =~ ^runs/[A-Za-z0-9._-]+$ ]]; then
  run_root="$APP_DIR/$run_root_rel"
  for rel in \
    runtime_supervisor.csv \
    allocator_status.json \
    runtime_status.json \
    strategy_status.csv \
    market_proxy_status.json \
    market_proxy.log \
    trade_recorder.log \
    recorder.log \
    multileg.log \
    hard_arb/status.json \
    hard_arb/runtime.log \
    local_factor_status.json; do
    path="$run_root/$rel"
    if [[ -f "$path" ]]; then
      safe_label="$(printf '%s' "$rel" | tr '/.' '__')"
      printf '[mac-deploy] candidate_file_%s_begin\n' "$safe_label"
      tail -n 40 "$path" 2>&1 || true
      printf '[mac-deploy] candidate_file_%s_end\n' "$safe_label"
    fi
  done
fi

printf '[mac-deploy] candidate_health_diagnostics_end\n'
exit 0
