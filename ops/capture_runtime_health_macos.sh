#!/usr/bin/env bash
set -u

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
TARGET_SHA="${1:-unknown}"
RUN_ROOT="$APP_DIR/runs/paper_v7_live"

printf '[mac-deploy-v7] candidate_health_diagnostics_begin\n'
printf '[mac-deploy-v7] candidate_expected_sha=%s\n' "$TARGET_SHA"
printf '[mac-deploy-v7] candidate_actual_sha=%s\n' "$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"

meta="$(python3 - "$APP_DIR/config/live_champion.json" <<'PY' 2>/dev/null || true
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
print(f"{m.get('version','')}\t{m.get('run_root','')}\t{m.get('config','')}\t{m.get('loop','')}")
PY
)"
IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
printf '[mac-deploy-v7] candidate_champion_version=%s\n' "${version:-unknown}"
printf '[mac-deploy-v7] candidate_run_root=%s\n' "${run_root_rel:-unknown}"

probe_http(){
  local label="$1" url="$2" payload
  payload="$(curl --silent --show-error --max-time 4 --write-out $'\n__HTTP_STATUS__:%{http_code}' "$url" 2>&1 || true)"
  printf '[mac-deploy-v7] %s_begin\n%s\n[mac-deploy-v7] %s_end\n' "$label" "$payload" "$label"
}
probe_http exporter_healthz http://127.0.0.1:9108/healthz
probe_http prometheus_ready http://127.0.0.1:9090/-/ready
probe_http grafana_health http://127.0.0.1:3000/api/health
probe_http grafana_search http://127.0.0.1:3000/api/search

metrics="$(curl --silent --show-error --max-time 4 http://127.0.0.1:9108/metrics 2>&1 || true)"
printf '[mac-deploy-v7] candidate_key_metrics_begin\n'
printf '%s\n' "$metrics" | grep -E '^(polymarket_runtime_info|polymarket_v7_runtime_info|polymarket_runtime_(equity_usd|pnl_usd|drawdown_ratio|kill_switch|execution_staleness_seconds)|polymarket_allocator_(state_present|models_expected|models_alive)|polymarket_model_(info|pnl_usd|fills_total|gross_exposure_usd|alert_staleness_seconds))' | head -n 160 || true
printf '[mac-deploy-v7] candidate_key_metrics_end\n'

if [[ -f "$APP_DIR/ops/macos_service_control.sh" ]]; then
  printf '[mac-deploy-v7] candidate_service_status_begin\n'
  bash "$APP_DIR/ops/macos_service_control.sh" status 2>&1 | tail -n 100 || true
  printf '[mac-deploy-v7] candidate_service_status_end\n'
fi

for rel in \
  v7_supervisor.json \
  execution/runtime_status.json \
  execution/allocator_status.json \
  execution/strategy_status.csv \
  execution/market_proxy_status.json \
  execution/market_proxy.log \
  execution/trade_recorder.log \
  execution/multileg.log \
  execution/hard_arb/status.json \
  execution/hard_arb.log \
  shadow/scheduler_status.json; do
  path="$RUN_ROOT/$rel"
  if [[ -f "$path" ]]; then
    safe_label="$(printf '%s' "$rel" | tr '/.' '__')"
    printf '[mac-deploy-v7] candidate_file_%s_begin\n' "$safe_label"
    tail -n 40 "$path" 2>&1 || true
    printf '[mac-deploy-v7] candidate_file_%s_end\n' "$safe_label"
  fi
done

printf '[mac-deploy-v7] candidate_processes_begin\n'
pgrep -alf 'paper_v7_loop|polymarket_trade_recorder|v7_multileg_broker_runner|v7_market_proxy|v7_shadow_loop' 2>/dev/null || true
printf '[mac-deploy-v7] candidate_processes_end\n'
printf '[mac-deploy-v7] candidate_health_diagnostics_end\n'
exit 0
