#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
TAILSCALE_HOSTNAME="${POLYMARKET_TAILSCALE_HOSTNAME:-mamma-portfolio}"
TAILSCALE_FQDN="${POLYMARKET_TAILSCALE_FQDN:-mamma-portfolio.tail1bae85.ts.net}"
POLYMARKET_GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_FQDN}}"
GRAFANA_ASSET_DIR="${POLYMARKET_GRAFANA_ASSET_DIR:-$APP_DIR/monitoring/grafana/dashboards}"
GRAFANA_STATE_DASHBOARD_DIR="$STATE_DIR/grafana/dashboards"
CANONICAL_DASHBOARD="polymarket-multi-strategy.json"

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
[[ -d "$GRAFANA_ASSET_DIR" ]] || {
  echo "missing Grafana asset directory: $GRAFANA_ASSET_DIR" >&2
  exit 1
}
[[ -f "$GRAFANA_ASSET_DIR/$CANONICAL_DASHBOARD" ]] || {
  echo "missing canonical Grafana dashboard: $GRAFANA_ASSET_DIR/$CANONICAL_DASHBOARD" >&2
  exit 1
}
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" config --replace-all remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
fi

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
  echo "python3 not found; cannot validate Grafana assets and Tailscale identity" >&2
  exit 1
}

"$PYTHON_BIN" - "$GRAFANA_ASSET_DIR" "$CANONICAL_DASHBOARD" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
canonical = sys.argv[2]
files = sorted(root.glob("*.json"))
if not files:
    raise SystemExit("Grafana asset directory contains no JSON dashboards")
for path in files:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"Grafana dashboard must be an object: {path}")
target = json.loads((root / canonical).read_text(encoding="utf-8"))
if not target.get("uid") or not target.get("title"):
    raise SystemExit("canonical Grafana dashboard must define stable uid and title")
print(f"canonical_dashboard_uid={target['uid']} title={target['title']}")
PY

mkdir -p "$STATE_DIR/grafana/provisioning/datasources" \
  "$STATE_DIR/grafana/provisioning/dashboards" \
  "$STATE_DIR/grafana"

# Snapshot the exact dashboard bundle into state. Grafana never reads the live
# git worktree, so a workflow can deploy assets from its exact SHA even when the
# runtime checkout is deliberately pinned to another validated revision.
snapshot="$(mktemp -d "$STATE_DIR/grafana/dashboards.next.XXXXXX")"
serve_log="$(mktemp)"
cleanup() {
  rm -rf "$snapshot" 2>/dev/null || true
  rm -f "$serve_log" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

copied=0
for source in "$GRAFANA_ASSET_DIR"/*.json; do
  [[ -f "$source" ]] || continue
  cp "$source" "$snapshot/"
  copied=$((copied + 1))
done
(( copied > 0 )) || {
  echo "no Grafana dashboards copied from $GRAFANA_ASSET_DIR" >&2
  exit 1
}
[[ -f "$snapshot/$CANONICAL_DASHBOARD" ]] || {
  echo "canonical Grafana dashboard missing from staged snapshot" >&2
  exit 1
}

previous="$STATE_DIR/grafana/dashboards.previous.$$"
rm -rf "$previous"
if [[ -d "$GRAFANA_STATE_DASHBOARD_DIR" ]]; then
  mv "$GRAFANA_STATE_DASHBOARD_DIR" "$previous"
fi
mv "$snapshot" "$GRAFANA_STATE_DASHBOARD_DIR"
snapshot=""
rm -rf "$previous"

dashboard_sha="$("$PYTHON_BIN" - "$GRAFANA_STATE_DASHBOARD_DIR/$CANONICAL_DASHBOARD" <<'PY'
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"

tailscale_admin() {
  if "$TAILSCALE_BIN" "$@"; then
    return 0
  fi
  sudo -n "$TAILSCALE_BIN" "$@"
}

tailscale_admin set --hostname="$TAILSCALE_HOSTNAME"

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
default_home_dashboard_path = $GRAFANA_STATE_DASHBOARD_DIR/$CANONICAL_DASHBOARD
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
    allowUiUpdates: false
    updateIntervalSeconds: 10
    options:
      path: "$GRAFANA_STATE_DASHBOARD_DIR"
EOF

printf 'grafana_mode=anonymous_viewer_no_login backend=127.0.0.1:3000 exposure=tailscale-serve operator_url=%s tailscale_hostname=%s tailscale_fqdn=%s dashboard_file=%s dashboard_sha256=%s\n' \
  "$POLYMARKET_GRAFANA_URL" "$TAILSCALE_HOSTNAME" "$TAILSCALE_FQDN" "$CANONICAL_DASHBOARD" "$dashboard_sha"
