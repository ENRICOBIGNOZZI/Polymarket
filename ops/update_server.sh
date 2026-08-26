#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOCAL_BRANCH="${POLYMARKET_BRANCH:-main}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"

log(){ printf '[deploy-v7] %s\n' "$*"; }
fail(){ printf '[deploy-v7] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout; run ops/bootstrap_server.sh once"
[[ -f "$APP_DIR/.server_bootstrapped" ]] || fail "server bootstrap marker missing"
cd "$APP_DIR"

OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || fail "$DEPLOY_REF is not an ancestor of $LOCAL_BRANCH"
if [[ "$OLD_SHA" != "$NEW_SHA" ]]; then
  git checkout "$LOCAL_BRANCH"
  git reset --hard "$NEW_SHA"
fi

readarray -t CHAMPION < <(python3 - <<'PY'
import json
from pathlib import Path
m=json.loads(Path('config/live_champion.json').read_text())
for key in ('version','loop','config','run_root'): print(m[key])
PY
)
VERSION="${CHAMPION[0]}"; LOOP_REL="${CHAMPION[1]}"; CONFIG_REL="${CHAMPION[2]}"; RUN_ROOT_REL="${CHAMPION[3]}"; RUN_NAME="$(basename "$RUN_ROOT_REL")"
[[ "$VERSION" == "7" ]] || fail "only V7 can be deployed; manifest selected V$VERSION"
[[ "$LOOP_REL" == "scripts/paper_v7_loop.sh" ]] || fail "unexpected V7 loop: $LOOP_REL"
[[ "$CONFIG_REL" == "config/paper_v7.json" ]] || fail "unexpected V7 config: $CONFIG_REL"
[[ "$RUN_ROOT_REL" == "runs/paper_v7_live" ]] || fail "unexpected V7 run root: $RUN_ROOT_REL"
[[ -f "$LOOP_REL" && -f "$CONFIG_REL" ]] || fail "V7 champion files are missing"

log "Building and validating exact V7 paper-validated revision $NEW_SHA"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest tests/test_monitoring_v7_exporter.py tests/test_grafana_v7_contract.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v7.py scripts/v7_*.py
bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh scripts/monitoring_up.sh scripts/monitoring_down.sh
python3 -m json.tool "$CONFIG_REL" >/dev/null
python3 -m json.tool config/live_champion.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
sudo docker compose -f docker-compose.monitoring.yml config >/dev/null

log "Installing V7-only systemd services"
DEPLOY_USER="$(id -un)"
sudo install -d -m 0755 /etc/polymarket
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
POLYMARKET_APP_DIR=$APP_DIR
POLYMARKET_RUN_NAME=$RUN_NAME
EOF
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
ExecStart=/usr/bin/env bash $APP_DIR/$LOOP_REL $APP_DIR/$CONFIG_REL $APP_DIR/$RUN_ROOT_REL
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
Environment=POLYMARKET_RUN_NAME=$RUN_NAME
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
metrics=""
for _ in {1..90}; do
  if sudo systemctl is-active --quiet polymarket-paper.service && \
     sudo systemctl is-active --quiet polymarket-monitoring.service && \
     curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1; then
    metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null || true)"
    if grep -q '^polymarket_runtime_info{adapter="v7",run_root="paper_v7_live",version="v7"} 1$' <<<"$metrics" && \
       grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics"; then
      healthy=1
      break
    fi
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || fail "V7 runtime did not become healthy"
grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics" || fail "V7 runtime PnL metric missing"
grep -q '^polymarket_allocator_state_present 1$' <<<"$metrics" || fail "V7 allocator state missing"
grep -q '^polymarket_allocator_models_expected 5$' <<<"$metrics" || fail "V7 model count mismatch"
grep -q '^polymarket_model_info{' <<<"$metrics" || fail "V7 per-strategy metrics missing"

test -s "$APP_DIR/$RUN_ROOT_REL/v7_supervisor.json" || fail "V7 supervisor missing"
test -s "$APP_DIR/$RUN_ROOT_REL/execution/runtime_status.json" || fail "V7 runtime status missing"
test -s "$APP_DIR/$RUN_ROOT_REL/execution/allocator_status.json" || fail "V7 allocator status missing"
test -s "$APP_DIR/$RUN_ROOT_REL/execution/strategy_status.csv" || fail "V7 strategy status missing"
test -s "$APP_DIR/$RUN_ROOT_REL/execution/hard_arb/status.json" || fail "V7 hard-arb status missing"
python3 - "$APP_DIR/$RUN_ROOT_REL" <<'PY'
import json,sys,time
from pathlib import Path
root=Path(sys.argv[1])
supervisor=json.loads((root/'v7_supervisor.json').read_text())
runtime=json.loads((root/'execution'/'runtime_status.json').read_text())
assert supervisor.get('execution_alive') is True, supervisor
assert supervisor.get('shadow_alive') is True, supervisor
assert runtime.get('version') == 7, runtime
assert runtime.get('paper_only') is True, runtime
assert runtime.get('authenticated_execution') is False, runtime
assert float(runtime.get('drawdown', 1.0)) <= 0.15 + 1e-12, runtime
assert time.time()-float(runtime['timestamp']) <= 180, runtime
PY

printf 'deployed_sha=%s\nvalidated_ref=%s\nmain_sha=%s\nprevious_sha=%s\nchampion_version=7\nchampion_run_root=%s\n' \
  "$NEW_SHA" "$DEPLOY_REF" "$MAIN_SHA" "$OLD_SHA" "$RUN_ROOT_REL"
