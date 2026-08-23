#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
[[ -d "$APP_DIR/monitoring/grafana/dashboards" ]] || {
  echo "missing Grafana dashboards under $APP_DIR" >&2
  exit 1
}

mkdir -p "$STATE_DIR/grafana/provisioning/datasources" \
  "$STATE_DIR/grafana/provisioning/dashboards"

# Grafana remains bound to loopback only. Anonymous Viewer access therefore
# removes the login prompt without exposing the dashboard on a public socket.
cat > "$STATE_DIR/grafana.ini" <<EOF
[server]
http_addr = 127.0.0.1
http_port = 3000

[users]
allow_sign_up = false

[auth.anonymous]
enabled = true
org_name = Main Org.
org_role = Viewer
hide_version = true

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

printf 'grafana_mode=anonymous_viewer bind=127.0.0.1:3000\n'
