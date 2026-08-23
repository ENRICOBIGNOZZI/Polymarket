#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${POLYMARKET_REPO_URL:-https://github.com/ENRICOBIGNOZZI/Polymarket.git}"
BRANCH="${POLYMARKET_BRANCH:-main}"
APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
RUN_NAME="${POLYMARKET_RUN_NAME:-paper_v4_live}"
CONFIG="${POLYMARKET_CONFIG:-config/paper_v4.json}"

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

log "Building Release"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"

log "Running deterministic tests"
ctest --test-dir build --output-on-failure
python3 -m unittest tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py tests/test_monitoring_latest_exporter.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_latest.py \
  scripts/build_v4_intents.py scripts/merge_v4_intents.py scripts/walk_forward_v4.py scripts/tiny_live_pilot.py
bash -n scripts/paper_v4_once.sh scripts/paper_v4_loop.sh scripts/monitoring_up.sh scripts/monitoring_down.sh

if docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.monitoring.yml config >/dev/null
else
  sudo docker compose -f docker-compose.monitoring.yml config >/dev/null 2>&1 || \
    fail "Docker Compose v2 is required for monitoring"
fi

log "Installing systemd services"
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
POLYMARKET_APP_DIR=$APP_DIR
POLYMARKET_RUN_NAME=$RUN_NAME
POLYMARKET_CONFIG=$CONFIG
EOF
sudo install -d -m 0755 /etc/polymarket
sudo install -m 0644 "$TMP_ENV" /etc/polymarket/runtime.env
rm -f "$TMP_ENV"

TMP_PAPER="$(mktemp)"
cat > "$TMP_PAPER" <<EOF
[Unit]
Description=Polymarket paper-live engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/polymarket/runtime.env
ExecStart=/usr/bin/env bash $APP_DIR/scripts/paper_v4_loop.sh $APP_DIR/$CONFIG $APP_DIR/runs/$RUN_NAME
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
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
Environment=POLYMARKET_RUN_NAME=$RUN_NAME
ExecStart=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml up -d
ExecStop=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml down
TimeoutStartSec=120
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP_MON" /etc/systemd/system/polymarket-monitoring.service
rm -f "$TMP_MON"

sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-paper.service
sudo systemctl enable --now polymarket-monitoring.service

log "Service status"
sudo systemctl --no-pager --full status polymarket-paper.service | sed -n '1,20p' || true
sudo systemctl --no-pager --full status polymarket-monitoring.service | sed -n '1,20p' || true

log "Bootstrap complete"
printf 'Repository: %s\n' "$APP_DIR"
printf 'Runtime:    %s\n' "$APP_DIR/runs/$RUN_NAME"
printf 'Grafana:    http://127.0.0.1:3000 (use an SSH tunnel)\n'
printf 'Paper logs: sudo journalctl -u polymarket-paper -f\n'
printf 'Monitor:    sudo journalctl -u polymarket-monitoring -f\n'
