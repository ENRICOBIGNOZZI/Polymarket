#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
TAILSCALE_HOSTNAME="${POLYMARKET_TAILSCALE_HOSTNAME:-mamma-portfolio}"
TAILSCALE_FQDN="${POLYMARKET_TAILSCALE_FQDN:-mamma-portfolio.tail1bae85.ts.net}"
POLYMARKET_GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_FQDN}}"

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
[[ -d "$APP_DIR/monitoring/grafana/dashboards" ]] || {
  echo "missing Grafana dashboards under $APP_DIR" >&2
  exit 1
}
[[ -f "$APP_DIR/monitoring/grafana/dashboards/polymarket-fast-paper.json" ]] || {
  echo "missing fast paper Grafana dashboard" >&2
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

find_python() {
  local candidate
  for candidate in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v python3 2>/dev/null || return 1
}

TAILSCALE_BIN="$(find_tailscale || true)"
[[ -n "$TAILSCALE_BIN" ]] || {
  echo "tailscale CLI not found; refusing to leave Grafana inaccessible" >&2
  exit 1
}
PYTHON_BIN="$(find_python || true)"
[[ -n "$PYTHON_BIN" ]] || {
  echo "python3 not found; cannot verify Tailscale DNS identity" >&2
  exit 1
}

tailscale_admin() {
  if "$TAILSCALE_BIN" "$@"; then
    return 0
  fi
  sudo -n "$TAILSCALE_BIN" "$@"
}

# Pin the server to the already-established Tailscale machine name. The FQDN
# returned by Tailscale is the canonical operator identity; do not invent a
# second alias for Grafana.
tailscale_admin set --hostname="$TAILSCALE_HOSTNAME"

# Wait for the control-plane/MagicDNS view to converge and verify the actual
# DNS identity before writing Grafana root_url.
actual_dns=""
for _ in {1..20}; do
  actual_dns="$("$TAILSCALE_BIN" status --json 2>/dev/null | \
    "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName", ""))' 2>/dev/null || true)"
  [[ "$actual_dns" == "$TAILSCALE_FQDN" || "$actual_dns" == "$TAILSCALE_FQDN." ]] && break
  sleep 1
done
if [[ "$actual_dns" != "$TAILSCALE_FQDN" && "$actual_dns" != "$TAILSCALE_FQDN." ]]; then
  echo "tailscale DNS mismatch: expected=$TAILSCALE_FQDN actual=${actual_dns:-<empty>}" >&2
  "$TAILSCALE_BIN" status || true
  exit 1
fi

serve_log="$(mktemp)"
trap 'rm -f "$serve_log"' EXIT
if tailscale_admin serve --bg --http=80 localhost:3000 >"$serve_log" 2>&1; then
  :
else
  cat "$serve_log" >&2
  echo "failed to configure Tailscale Serve for Grafana" >&2
  exit 1
fi

serve_status="$("$TAILSCALE_BIN" serve status 2>&1 || true)"
printf '%s\n' "$serve_status"
if ! grep -Fq "http://${TAILSCALE_FQDN}" <<<"$serve_status"; then
  echo "Tailscale Serve did not publish expected URL http://${TAILSCALE_FQDN}" >&2
  exit 1
fi

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
default_home_dashboard_path = $APP_DIR/monitoring/grafana/dashboards/polymarket-fast-paper.json
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

printf 'grafana_mode=anonymous_viewer_no_login backend=127.0.0.1:3000 exposure=tailscale-serve operator_url=%s tailscale_hostname=%s tailscale_fqdn=%s default_dashboard=polymarket-fast-paper\n' \
  "$POLYMARKET_GRAFANA_URL" "$TAILSCALE_HOSTNAME" "$TAILSCALE_FQDN"
