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
  log "Installing build/runtime dependencies"
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git ca-certificates curl build-essential cmake pkg-config \
    libcurl4-openssl-dev libboost-all-dev python3 docker.io
  if ! docker compose version >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 2>/dev/null || \
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin 2>/dev/null || true
  fi
else
  fail "This bootstrap currently supports Debian/Ubuntu apt-based servers"
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$DEPLOY_USER" || true

if [[ -d "$APP_DIR/.git" ]]; then
  log "Updating existing checkout in $APP_DIR"
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  log "Cloning $REPO_URL into $APP_DIR"
  rm -rf "$APP_DIR"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
readarray -t CHAMPION < <(python3 - <<'PY'
import json
from pathlib import Path
m = json.loads(Path('config/live_champion.json').read_text())
for key in ('version', 'loop', 'config', 'run_root'):
    print(m[key])
PY
)
VERSION="${CHAMPION[0]}"
RUN_NAME="$(basename "${CHAMPION[3]}")"

log "Building Release champion V$VERSION"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"

log "Running deterministic validation"
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

if docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.monitoring.yml config >/dev/null
else
  sudo docker compose -f docker-compose.monitoring.yml config >/dev/null 2>&1 || \
    fail "Docker Compose v2 is required for monitoring"
fi

log "Installing manifest-selected systemd services"
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

log "Installing narrowly-scoped passwordless deploy restarts"
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

log "Service status"
sudo systemctl --no-pager --full status polymarket-paper.service | sed -n '1,20p' || true
sudo systemctl --no-pager --full status polymarket-monitoring.service | sed -n '1,20p' || true

log "Bootstrap complete"
printf 'Repository: %s\n' "$APP_DIR"
printf 'Champion:   V%s\n' "$VERSION"
printf 'Runtime:    %s\n' "$APP_DIR/runs/$RUN_NAME"
printf 'Grafana:    http://127.0.0.1:3000 (use an SSH tunnel)\n'
printf 'Paper logs: sudo journalctl -u polymarket-paper -f\n'
printf 'Monitor:    sudo journalctl -u polymarket-monitoring -f\n'
printf 'NOTE: reconnect once so your docker-group membership is refreshed.\n'
