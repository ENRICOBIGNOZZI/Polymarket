#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOCAL_BRANCH="${POLYMARKET_BRANCH:-main}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"

log() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout; run ops/bootstrap_server.sh once"
[[ -f "$APP_DIR/.server_bootstrapped" ]] || fail "server bootstrap marker missing; run ops/bootstrap_server.sh once interactively"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || fail "$DEPLOY_REF ($NEW_SHA) is not an ancestor of $LOCAL_BRANCH ($MAIN_SHA)"

if [[ "$OLD_SHA" != "$NEW_SHA" ]]; then
  log "Updating $OLD_SHA -> validated V7 commit $NEW_SHA"
  git checkout "$LOCAL_BRANCH"
  git reset --hard "$NEW_SHA"
fi

python3 - <<'PY'
import json
m=json.load(open('config/live_champion.json'))
assert int(m['version']) == 7, m
assert m['loop'] == 'scripts/paper_v7_loop.sh', m
assert m['config'] == 'config/paper_v7.json', m
assert m['run_root'] == 'runs/paper_v7_live', m
cfg=json.load(open('config/paper_v7.json'))
assert cfg['paper_only'] is True
assert cfg['v7']['authenticated_execution'] is False
PY

log "Building and validating V7 only"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest tests/test_monitoring_v7_exporter.py tests/test_v7_unified_runtime.py tests/test_v7_point_in_time_archive_workflow.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v7.py monitoring/exporter_latest_v7.py scripts/v7_*.py
bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh scripts/monitoring_up.sh scripts/monitoring_down.sh
python3 -m json.tool config/paper_v7.json >/dev/null
python3 -m json.tool config/live_champion.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
sudo docker compose -f docker-compose.monitoring.yml config >/dev/null

DEPLOY_USER="$(id -un)"
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
POLYMARKET_APP_DIR=$APP_DIR
POLYMARKET_RUN_NAME=paper_v7_live
EOF
sudo install -d -m 0755 /etc/polymarket
sudo install -m 0644 "$TMP_ENV" /etc/polymarket/runtime.env
rm -f "$TMP_ENV"

TMP_PAPER="$(mktemp)"
cat > "$TMP_PAPER" <<EOF
[Unit]
Description=Polymarket V7 PAPER engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/polymarket/runtime.env
ExecStart=/usr/bin/env bash $APP_DIR/scripts/paper_v7_loop.sh $APP_DIR/config/paper_v7.json $APP_DIR/runs/paper_v7_live
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP_PAPER" /etc/systemd/system/polymarket-paper.service
rm -f "$TMP_PAPER"

TMP_MON="$(mktemp)"
cat > "$TMP_MON" <<EOF
[Unit]
Description=Polymarket V7 Prometheus/Grafana monitoring
After=docker.service polymarket-paper.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR
Environment=POLYMARKET_RUN_NAME=paper_v7_live
ExecStart=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml up -d --force-recreate
ExecStop=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml down
TimeoutStartSec=120
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP_MON" /etc/systemd/system/polymarket-monitoring.service
rm -f "$TMP_MON"
sudo systemctl daemon-reload
sudo systemctl restart polymarket-paper.service
sudo systemctl restart polymarket-monitoring.service

healthy=0
for _ in {1..90}; do
  if sudo systemctl is-active --quiet polymarket-paper.service && \
     sudo systemctl is-active --quiet polymarket-monitoring.service && \
     curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1; then
    metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null || true)"
    if grep -q '^polymarket_runtime_info{adapter="v7",run_root="paper_v7_live",version="v7"} 1$' <<<"$metrics"; then healthy=1; break; fi
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || fail "V7 runtime did not become healthy"

grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics"
grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics"
grep -q '^polymarket_allocator_state_present 1$' <<<"$metrics"
grep -q '^polymarket_allocator_models_expected 5$' <<<"$metrics"
grep -q '^polymarket_allocator_models_alive 5$' <<<"$metrics"
grep -q '^polymarket_model_info{' <<<"$metrics"

ROOT="$APP_DIR/runs/paper_v7_live"
EX="$ROOT/execution"
test -s "$ROOT/v7_supervisor.json"
test -s "$EX/v7_execution_supervisor.json"
test -s "$EX/runtime_status.json"
test -s "$EX/allocator_status.json"
test -s "$EX/strategy_status.csv"
test -s "$EX/market_proxy_status.json"
python3 - "$ROOT" <<'PY'
import csv,json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); ex=root/'execution'; now=time.time()
sup=json.load(open(root/'v7_supervisor.json')); exe=json.load(open(ex/'v7_execution_supervisor.json'))
runtime=json.load(open(ex/'runtime_status.json')); allocator=json.load(open(ex/'allocator_status.json')); proxy=json.load(open(ex/'market_proxy_status.json'))
rows=list(csv.DictReader(open(ex/'strategy_status.csv')))
assert sup['execution_alive'] is True and sup['shadow_alive'] is True and now-float(sup['timestamp']) <= 60
assert exe['paper_only'] is True and now-float(exe['timestamp']) <= 120
assert runtime['schema']=='polymarket_v7_runtime_status_v1' and runtime['version']==7 and runtime['paper_only'] is True and runtime['authenticated_execution'] is False
assert float(runtime['drawdown']) <= 0.15 + 1e-12
assert allocator['schema']=='polymarket_v7_allocator_status_v1' and int(allocator['models_expected'])==5 and int(allocator['models_alive'])==5
assert {r['name'] for r in rows} == {'micro_maker','micro_taker','relative_value','hard_arb','external'}
assert proxy['schema']=='polymarket_v7_market_proxy_status_v1' and now-float(proxy['timestamp']) <= 180
PY

printf 'deployed_sha=%s\nvalidated_ref=%s\nmain_sha=%s\nprevious_sha=%s\nchampion_version=7\nchampion_run_root=runs/paper_v7_live\n' "$NEW_SHA" "$DEPLOY_REF" "$MAIN_SHA" "$OLD_SHA"
