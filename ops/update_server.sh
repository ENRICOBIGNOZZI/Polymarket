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

log "Fetching origin/$LOCAL_BRANCH and validated ref origin/$DEPLOY_REF"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || \
  fail "$DEPLOY_REF ($NEW_SHA) is not an ancestor of $LOCAL_BRANCH ($MAIN_SHA)"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  log "Already at validated commit $NEW_SHA; revalidating and repairing services if needed"
else
  log "Updating $OLD_SHA -> validated commit $NEW_SHA"
  git checkout "$LOCAL_BRANCH"
  git reset --hard "$NEW_SHA"
fi

readarray -t CHAMPION < <(python3 - <<'PY'
import json
from pathlib import Path
m = json.loads(Path('config/live_champion.json').read_text())
for key in ('version', 'loop', 'config', 'run_root'):
    print(m[key])
PY
)
VERSION="${CHAMPION[0]}"
LOOP_REL="${CHAMPION[1]}"
CONFIG_REL="${CHAMPION[2]}"
RUN_ROOT_REL="${CHAMPION[3]}"
RUN_NAME="$(basename "$RUN_ROOT_REL")"
[[ "$VERSION" =~ ^[0-9]+$ ]] || fail "invalid champion version"
[[ -f "$LOOP_REL" && -f "$CONFIG_REL" ]] || fail "champion files are missing"

log "Building and validating paper-validated V$VERSION"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest \
  tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py \
  tests/test_monitoring_latest_exporter.py tests/test_monitoring_v5_exporter.py \
  tests/test_grafana_fast_paper_contract.py tests/test_grafana_multi_strategy_contract.py \
  tests/test_multi_strategy_paper.py -v
python3 -m py_compile \
  monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_v5.py monitoring/exporter_latest.py \
  scripts/multi_strategy_paper.py scripts/build_v4_intents.py scripts/merge_v4_intents.py \
  scripts/walk_forward_v4.py scripts/tiny_live_pilot.py
bash -n scripts/paper_latest_loop.sh scripts/paper_v5_loop.sh \
  scripts/monitoring_up.sh scripts/monitoring_down.sh
python3 -m json.tool config/paper_v5.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
sudo docker compose -f docker-compose.monitoring.yml config >/dev/null

log "Migrating systemd services to manifest-selected runtime"
DEPLOY_USER="$(id -un)"
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
POLYMARKET_APP_DIR=$APP_DIR
POLYMARKET_RUN_NAME=auto
EOF
sudo install -d -m 0755 /etc/polymarket
sudo install -m 0644 "$TMP_ENV" /etc/polymarket/runtime.env
rm -f "$TMP_ENV"

TMP_PAPER="$(mktemp)"
cat > "$TMP_PAPER" <<EOF
[Unit]
Description=Polymarket manifest-selected paper-live engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/polymarket/runtime.env
ExecStart=/usr/bin/env bash $APP_DIR/scripts/paper_latest_loop.sh
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
Description=Polymarket Prometheus/Grafana monitoring
After=docker.service polymarket-paper.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR
Environment=POLYMARKET_RUN_NAME=auto
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

log "Restarting V$VERSION paper and monitoring services"
sudo systemctl restart polymarket-paper.service
sudo systemctl restart polymarket-monitoring.service

healthy=0
for _ in {1..60}; do
  if sudo systemctl is-active --quiet polymarket-paper.service && \
     sudo systemctl is-active --quiet polymarket-monitoring.service && \
     curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1; then
    metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null || true)"
    if grep -q "^polymarket_runtime_info{adapter=\"v$VERSION\",run_root=\"$RUN_NAME\",version=\"v$VERSION\"} 1$" <<<"$metrics"; then
      healthy=1
      break
    fi
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || fail "V$VERSION runtime did not become healthy"

grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics"
if (( VERSION >= 5 )); then
  grep -q '^polymarket_allocator_state_present 1$' <<<"$metrics"
  grep -q '^polymarket_allocator_models_expected 5$' <<<"$metrics"
  grep -q '^polymarket_model_info{' <<<"$metrics"
fi

supervisor="$APP_DIR/$RUN_ROOT_REL/runtime_supervisor.csv"
test -s "$supervisor"
python3 - "$supervisor" "$VERSION" <<'PY'
import csv
import sys
import time
from pathlib import Path
path = Path(sys.argv[1])
version = int(sys.argv[2])
with path.open(newline='', encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
assert rows, 'empty runtime supervisor'
row = rows[-1]
assert row.get('recorder_alive') == '1', row
assert row.get('broker_alive') == '1', row
primary = 'allocator_alive' if version >= 5 else 'terminal_alive'
assert row.get(primary) == '1', row
assert time.time() - float(row['timestamp']) <= 60, row
PY

printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'validated_ref=%s\n' "$DEPLOY_REF"
printf 'main_sha=%s\n' "$MAIN_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
printf 'champion_version=%s\n' "$VERSION"
printf 'champion_run_root=%s\n' "$RUN_ROOT_REL"
