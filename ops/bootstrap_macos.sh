#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${POLYMARKET_REPO_URL:-https://github.com/ENRICOBIGNOZZI/Polymarket.git}"
BRANCH="${POLYMARKET_BRANCH:-main}"
APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOG_DIR="${POLYMARKET_LOG_DIR:-$HOME/Library/Logs/Polymarket}"
RUN_ROOT="$APP_DIR/runs/paper_v7_live"

log(){ printf '[mac-bootstrap-v7] %s\n' "$*"; }
fail(){ printf '[mac-bootstrap-v7] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS only"
command -v sudo >/dev/null 2>&1 || fail "sudo is required"

if ! command -v brew >/dev/null 2>&1; then
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  [[ -x /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
  [[ -x /usr/local/bin/brew ]] && eval "$(/usr/local/bin/brew shellenv)"
fi
command -v brew >/dev/null 2>&1 || fail "Homebrew installation not found"
BREW_PREFIX="$(brew --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
brew update
brew install git cmake pkg-config boost curl python prometheus grafana

mkdir -p "$STATE_DIR" "$LOG_DIR" "$STATE_DIR/grafana/provisioning/datasources" \
  "$STATE_DIR/grafana/provisioning/dashboards" "$APP_DIR/runs/monitoring/prometheus" \
  "$APP_DIR/runs/monitoring/grafana/data" "$APP_DIR/runs/monitoring/grafana/logs" \
  "$APP_DIR/runs/monitoring/grafana/plugins"

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
from pathlib import Path
m=json.loads(Path('config/live_champion.json').read_text())
assert int(m['version']) == 7, m
assert m['loop'] == 'scripts/paper_v7_loop.sh', m
assert m['config'] == 'config/paper_v7.json', m
assert m['run_root'] == 'runs/paper_v7_live', m
assert m.get('paper_only') is True and m.get('authenticated_execution') is False, m
PY

export PKG_CONFIG_PATH="$(brew --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
JOBS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
cmake --build build --parallel "$JOBS"
ctest --test-dir build --output-on-failure
"$BREW_PREFIX/bin/python3" -m unittest tests/test_monitoring_v7_exporter.py tests/test_grafana_v7_contract.py -v
"$BREW_PREFIX/bin/python3" -m py_compile monitoring/exporter.py monitoring/exporter_v7.py scripts/v7_*.py
bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh ops/apply_runtime_config_macos.sh
python3 -m json.tool config/paper_v7.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null

cat > "$STATE_DIR/prometheus.yml" <<EOF
global:
  scrape_interval: 5s
  evaluation_interval: 5s
rule_files:
  - "$APP_DIR/monitoring/prometheus/alerts.yml"
scrape_configs:
  - job_name: polymarket-exporter
    static_configs:
      - targets: ["127.0.0.1:9108"]
EOF
"$BREW_PREFIX/bin/promtool" check config "$STATE_DIR/prometheus.yml"
bash "$APP_DIR/ops/apply_runtime_config_macos.sh"

PASSWORD_FILE="$STATE_DIR/grafana-admin-password"
if [[ ! -s "$PASSWORD_FILE" ]]; then /usr/bin/openssl rand -hex 18 > "$PASSWORD_FILE"; chmod 600 "$PASSWORD_FILE"; fi
GRAFANA_PASSWORD="$(cat "$PASSWORD_FILE")"
PYTHON_BIN="$BREW_PREFIX/bin/python3"
PROM_BIN="$BREW_PREFIX/bin/prometheus"
GRAFANA_BIN="$BREW_PREFIX/bin/grafana"
GRAFANA_HOME="$(brew --prefix grafana)/share/grafana"

xml_escape(){ printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'; }
write_plist(){
  local label="$1" user="$2" program="$3" workdir="$4" stdout="$5" stderr="$6"; shift 6
  local tmp="$(mktemp)"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(xml_escape "$label")</string>
  <key>UserName</key><string>$(xml_escape "$user")</string>
  <key>ProgramArguments</key><array>
    <string>$(xml_escape "$program")</string>
EOF
    for arg in "$@"; do printf '    <string>%s</string>\n' "$(xml_escape "$arg")"; done
    cat <<EOF
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$workdir")</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$(xml_escape "$HOME")</string>
    <key>PATH</key><string>$(xml_escape "$PATH")</string>
    <key>PKG_CONFIG_PATH</key><string>$(xml_escape "$PKG_CONFIG_PATH")</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$stdout")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$stderr")</string>
</dict></plist>
EOF
  } > "$tmp"
  /usr/bin/plutil -lint "$tmp" >/dev/null
  sudo install -o root -g wheel -m 0644 "$tmp" "/Library/LaunchDaemons/$label.plist"
  rm -f "$tmp"
}

write_plist com.polymarket.awake "$USER" /usr/bin/caffeinate "$HOME" "$LOG_DIR/awake.out.log" "$LOG_DIR/awake.err.log" -ims
write_plist com.polymarket.paper "$USER" /bin/bash "$APP_DIR" "$LOG_DIR/paper.out.log" "$LOG_DIR/paper.err.log" \
  "$APP_DIR/scripts/paper_v7_loop.sh" "$APP_DIR/config/paper_v7.json" "$RUN_ROOT"
write_plist com.polymarket.exporter "$USER" "$PYTHON_BIN" "$APP_DIR" "$LOG_DIR/exporter.out.log" "$LOG_DIR/exporter.err.log" \
  "$APP_DIR/monitoring/exporter_v7.py" --runs-base "$APP_DIR/runs" --run-name paper_v7_live \
  --config-dir "$APP_DIR/config" --config paper_v7.json --host 127.0.0.1 --port 9108 --top-opportunities 20
write_plist com.polymarket.prometheus "$USER" "$PROM_BIN" "$APP_DIR" "$LOG_DIR/prometheus.out.log" "$LOG_DIR/prometheus.err.log" \
  "--config.file=$STATE_DIR/prometheus.yml" "--storage.tsdb.path=$APP_DIR/runs/monitoring/prometheus" \
  --web.listen-address=127.0.0.1:9090 --web.enable-lifecycle

GRAFANA_PLIST="$(mktemp)"
cat > "$GRAFANA_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.polymarket.grafana</string>
  <key>UserName</key><string>$(xml_escape "$USER")</string>
  <key>ProgramArguments</key><array>
    <string>$(xml_escape "$GRAFANA_BIN")</string><string>server</string>
    <string>--homepath=$(xml_escape "$GRAFANA_HOME")</string>
    <string>--config=$(xml_escape "$STATE_DIR/grafana.ini")</string>
    <string>cfg:default.paths.data=$(xml_escape "$APP_DIR/runs/monitoring/grafana/data")</string>
    <string>cfg:default.paths.logs=$(xml_escape "$APP_DIR/runs/monitoring/grafana/logs")</string>
    <string>cfg:default.paths.plugins=$(xml_escape "$APP_DIR/runs/monitoring/grafana/plugins")</string>
    <string>cfg:default.paths.provisioning=$(xml_escape "$STATE_DIR/grafana/provisioning")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$APP_DIR")</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$(xml_escape "$HOME")</string><key>PATH</key><string>$(xml_escape "$PATH")</string>
    <key>GF_SECURITY_ADMIN_USER</key><string>admin</string><key>GF_SECURITY_ADMIN_PASSWORD</key><string>$(xml_escape "$GRAFANA_PASSWORD")</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_DIR/grafana.out.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_DIR/grafana.err.log")</string>
</dict></plist>
EOF
/usr/bin/plutil -lint "$GRAFANA_PLIST" >/dev/null
sudo install -o root -g wheel -m 0644 "$GRAFANA_PLIST" /Library/LaunchDaemons/com.polymarket.grafana.plist
rm -f "$GRAFANA_PLIST"

for label in com.polymarket.awake com.polymarket.paper com.polymarket.exporter com.polymarket.prometheus com.polymarket.grafana; do
  sudo launchctl bootout "system/$label" 2>/dev/null || true
  sudo launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
done

sudo install -d -o root -g wheel -m 0755 /usr/local/sbin
sudo install -o root -g wheel -m 0755 "$APP_DIR/ops/macos_service_control.sh" /usr/local/sbin/polymarket-service-control
SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/polymarket-service-control *\n' "$USER" > "$SUDOERS_TMP"
sudo install -d -o root -g wheel -m 0755 /etc/sudoers.d
sudo install -o root -g wheel -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/polymarket-deploy
rm -f "$SUDOERS_TMP"
sudo visudo -cf /etc/sudoers.d/polymarket-deploy >/dev/null
touch "$APP_DIR/.server_bootstrapped_macos"

for _ in {1..60}; do
  if curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1; then break; fi
  sleep 2
done
metrics="$(curl -fsS http://127.0.0.1:9108/metrics)" || fail "V7 exporter health check failed"
grep -q '^polymarket_runtime_info{adapter="v7",run_root="paper_v7_live",version="v7"} 1$' <<<"$metrics" || fail "V7 runtime metrics missing"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null || fail "Prometheus not ready"
curl -fsS http://127.0.0.1:3000/api/health >/dev/null || fail "Grafana not healthy"
curl -fsS http://127.0.0.1:3000/api/search | grep -q 'polymarket-v7-paper' || fail "V7 Grafana dashboard missing"

log "V7 bootstrap complete"
printf 'Repository: %s\nGit head: %s\nRuntime: %s\nGrafana: http://127.0.0.1:3000\n' "$APP_DIR" "$(git rev-parse HEAD)" "$RUN_ROOT"
