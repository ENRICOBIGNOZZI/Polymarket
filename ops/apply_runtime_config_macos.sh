#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
TAILSCALE_HOSTNAME="${POLYMARKET_TAILSCALE_HOSTNAME:-polymarket}"
POLYMARKET_GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_HOSTNAME}}"

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
[[ -d "$APP_DIR/monitoring/grafana/dashboards" ]] || {
  echo "missing Grafana dashboards under $APP_DIR" >&2
  exit 1
}

find_tailscale() {
  local candidate
  for candidate in \
    /Applications/Tailscale.app/Contents/MacOS/Tailscale \
    /opt/homebrew/bin/tailscale \
    /usr/local/bin/tailscale; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v tailscale 2>/dev/null || return 1
}

mkdir -p "$STATE_DIR/grafana/provisioning/datasources" \
  "$STATE_DIR/grafana/provisioning/dashboards"

cat > "$STATE_DIR/grafana.ini" <<EOF
[server]
http_addr = 127.0.0.1
http_port = 3000
root_url = ${POLYMARKET_GRAFANA_URL}/

[users]
allow_sign_up = false
auto_assign_org = true
auto_assign_org_id = 1
auto_assign_org_role = Viewer

[auth]
disable_login_form = true
disable_signout_menu = true

[auth.basic]
enabled = false

[auth.anonymous]
enabled = true
org_name = Main Org.
org_role = Viewer
hide_version = true

[analytics]
reporting_enabled = false
check_for_updates = false
check_for_plugin_updates = false

[news]
news_feed_enabled = false

[dashboards]
default_home_dashboard_path = $APP_DIR/monitoring/grafana/dashboards/polymarket-latest.json
EOF

cat > "$STATE_DIR/grafana/provisioning/datasources/prometheus.yml" <<'EOF'
apiVersion: 1
prune: true
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    editable: false
    jsonData:
      timeInterval: 5s
EOF

cat > "$STATE_DIR/grafana/provisioning/dashboards/dashboards.yml" <<EOF
apiVersion: 1
providers:
  - name: polymarket
    orgId: 1
    folder: Polymarket
    type: file
    disableDeletion: false
    allowUiUpdates: true
    updateIntervalSeconds: 10
    options:
      path: "$APP_DIR/monitoring/grafana/dashboards"
EOF

TAILSCALE_BIN="$(find_tailscale || true)"
[[ -n "$TAILSCALE_BIN" ]] || {
  echo "tailscale CLI not found; refusing to leave Grafana inaccessible" >&2
  exit 1
}

tailscale_admin() {
  if "$TAILSCALE_BIN" "$@"; then
    return 0
  fi
  sudo -n "$TAILSCALE_BIN" "$@"
}

# Give the paper server a permanent MagicDNS identity. This decouples the
# operator URL from the node's 100.x address and from Grafana's backend port.
tailscale_admin set --hostname="$TAILSCALE_HOSTNAME"

serve_log="$(mktemp)"
trap 'rm -f "$serve_log"' EXIT
if tailscale_admin serve --bg --http=80 localhost:3000 >"$serve_log" 2>&1; then
  :
else
  cat "$serve_log" >&2
  echo "failed to configure Tailscale Serve for Grafana" >&2
  exit 1
fi

printf 'grafana_mode=anonymous_viewer_no_login backend=127.0.0.1:3000 exposure=tailscale-serve operator_url=%s tailscale_hostname=%s\n' \
  "$POLYMARKET_GRAFANA_URL" "$TAILSCALE_HOSTNAME"
