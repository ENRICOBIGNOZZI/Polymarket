#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${POLYMARKET_REPO_URL:-https://github.com/ENRICOBIGNOZZI/Polymarket.git}"
BRANCH="${POLYMARKET_BRANCH:-main}"
APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
DEPLOY_USER="$(id -un)"

log() { printf '[bootstrap] %s\n' "$*"; }
fail() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

command -v sudo >/dev/null 2>&1 || fail "sudo is required"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates curl build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3 docker.io
  if ! docker compose version >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 2>/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin 2>/dev/null || true
  fi
else
  fail "This bootstrap supports Debian/Ubuntu apt-based servers"
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$DEPLOY_USER" || true

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
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

log "Building V7"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest tests/test_monitoring_v7_exporter.py tests/test_v7_unified_runtime.py tests/test_v7_point_in_time_archive_workflow.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v7.py monitoring/exporter_latest_v7.py scripts/v7_*.py
bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh scripts/monitoring_up.sh scripts/monitoring_down.sh
python3 -m json.tool config/paper_v7.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
if docker compose version >/dev/null 2>&1; then docker compose -f docker-compose.monitoring.yml config >/dev/null
else sudo docker compose -f docker-compose.monitoring.yml config >/dev/null 2>&1 || fail "Docker Compose v2 is required"; fi

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

SYSTEMCTL="$(command -v systemctl)"
TMP_SUDOERS="$(mktemp)"
cat > "$TMP_SUDOERS" <<EOF
Cmnd_Alias POLYMARKET_SYSTEMD = $SYSTEMCTL restart polymarket-paper.service, $SYSTEMCTL restart polymarket-monitoring.service, $SYSTEMCTL is-active polymarket-paper.service, $SYSTEMCTL is-active polymarket-monitoring.service, $SYSTEMCTL status polymarket-paper.service, $SYSTEMCTL status polymarket-monitoring.service, $SYSTEMCTL daemon-reload
$DEPLOY_USER ALL=(root) NOPASSWD: POLYMARKET_SYSTEMD
EOF
sudo visudo -cf "$TMP_SUDOERS" >/dev/null
sudo install -m 0440 "$TMP_SUDOERS" /etc/sudoers.d/polymarket-deploy
rm -f "$TMP_SUDOERS"

sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-paper.service
sudo systemctl enable --now polymarket-monitoring.service
touch "$APP_DIR/.server_bootstrapped"

log "Bootstrap complete"
printf 'Repository: %s\nChampion: V7\nRuntime: %s\nGrafana: http://127.0.0.1:3000\n' "$APP_DIR" "$APP_DIR/runs/paper_v7_live"
