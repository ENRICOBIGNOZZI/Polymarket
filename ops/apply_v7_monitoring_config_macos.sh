#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${POLYMARKET_APP_DIR:-$ROOT}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
MANIFEST="$APP_DIR/monitoring/v7_monitoring_manifest.json"
TAILSCALE_HOSTNAME="${POLYMARKET_TAILSCALE_HOSTNAME:-mamma-portfolio}"
TAILSCALE_FQDN="${POLYMARKET_TAILSCALE_FQDN:-mamma-portfolio.tail1bae85.ts.net}"
GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_FQDN}}"

[[ "$(uname -s)" == "Darwin" ]] || { echo "fatal: macOS monitoring installer requires Darwin" >&2; exit 78; }
[[ -f "$MANIFEST" ]] || { echo "fatal: missing V7 monitoring manifest" >&2; exit 78; }

stop_stale_grafana_listener() {
  local lsof_bin="" pids="" pid="" command_line=""
  for candidate in /usr/sbin/lsof /usr/bin/lsof; do
    if [[ -x "$candidate" ]]; then lsof_bin="$candidate"; break; fi
  done
  if [[ -z "$lsof_bin" ]] && command -v lsof >/dev/null 2>&1; then
    lsof_bin="$(command -v lsof)"
  fi
  [[ -n "$lsof_bin" ]] || return 0

  pids="$("$lsof_bin" -nP -tiTCP:3000 -sTCP:LISTEN 2>/dev/null || true)"
  for pid in $pids; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    case "$command_line" in
      *grafana*|*Grafana*)
        printf '[v7-monitoring] stopping stale Grafana listener pid=%s on 127.0.0.1:3000\n' "$pid" >&2
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
          kill -KILL "$pid" 2>/dev/null || true
        fi
        ;;
      *)
        printf 'fatal: canonical Grafana port 3000 is owned by non-Grafana pid=%s command=%s\n' "$pid" "$command_line" >&2
        exit 78
        ;;
    esac
  done
}

find_tailscale() {
  for candidate in \
    /Applications/Tailscale.app/Contents/MacOS/Tailscale \
    /opt/homebrew/bin/tailscale \
    /usr/local/bin/tailscale; do
    if [[ -x "$candidate" ]]; then printf '%s\n' "$candidate"; return 0; fi
  done
  command -v tailscale 2>/dev/null || return 1
}

tailscale_admin() {
  local binary="$1"
  shift
  if "$binary" "$@"; then
    return 0
  fi
  sudo -n "$binary" "$@"
}

configure_tailnet_grafana() {
  local ts actual_dns="" serve_status="" serve_log=""
  ts="$(find_tailscale || true)"
  [[ -n "$ts" ]] || { echo "fatal: tailscale CLI not found; Grafana would remain loopback-only" >&2; exit 78; }

  tailscale_admin "$ts" set --hostname="$TAILSCALE_HOSTNAME" >/dev/null
  for _ in $(seq 1 20); do
    actual_dns="$("$ts" status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName", ""))' 2>/dev/null || true)"
    [[ "$actual_dns" == "$TAILSCALE_FQDN" || "$actual_dns" == "$TAILSCALE_FQDN." ]] && break
    sleep 1
  done
  if [[ "$actual_dns" != "$TAILSCALE_FQDN" && "$actual_dns" != "$TAILSCALE_FQDN." ]]; then
    printf 'fatal: tailscale DNS mismatch expected=%s actual=%s\n' "$TAILSCALE_FQDN" "${actual_dns:-<empty>}" >&2
    exit 78
  fi

  serve_log="$(mktemp)"
  if ! tailscale_admin "$ts" serve --bg --http=80 localhost:3000 >"$serve_log" 2>&1; then
    cat "$serve_log" >&2 || true
    rm -f "$serve_log"
    echo "fatal: failed to configure Tailscale Serve for V7 Grafana" >&2
    exit 78
  fi
  rm -f "$serve_log"
  serve_status="$("$ts" serve status 2>&1 || true)"
  printf '%s\n' "$serve_status"
  if ! grep -Fq "$TAILSCALE_FQDN" <<<"$serve_status"; then
    printf 'fatal: Tailscale Serve did not publish expected FQDN %s\n' "$TAILSCALE_FQDN" >&2
    exit 78
  fi
}

stop_stale_grafana_listener

read -r DASHBOARD_FILE DATASOURCE_FILE PROVIDER_FILE PROMETHEUS_FILE ALERT_RULES_FILE DASHBOARD_UID < <(
  python3 - "$MANIFEST" <<'PY'
import json,sys
from pathlib import Path, PurePosixPath
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
assert manifest.get('schema') == 'polymarket_v7_monitoring_manifest_v2'
assert manifest.get('version') == 7
assert manifest.get('paper_only') is True
assert manifest.get('authenticated_execution') is False

def safe(value):
    text=str(value or '')
    path=PurePosixPath(text)
    assert text and not path.is_absolute() and '..' not in path.parts
    return text

graf=manifest['grafana']; prom=manifest['prometheus']
print(
    safe(graf['dashboard_file']),
    safe(graf['datasource_file']),
    safe(graf['provider_file']),
    safe(prom['config']),
    safe(prom['alert_rules']),
    str(graf['dashboard_uid']),
)
PY
)

for rel in "$DASHBOARD_FILE" "$DATASOURCE_FILE" "$PROVIDER_FILE" "$PROMETHEUS_FILE" "$ALERT_RULES_FILE"; do
  [[ -f "$APP_DIR/$rel" ]] || { echo "fatal: missing V7 monitoring asset: $rel" >&2; exit 78; }
done

python3 - "$APP_DIR/$DASHBOARD_FILE" "$DASHBOARD_UID" <<'PY'
import json,sys
from pathlib import Path
path=Path(sys.argv[1]); expected=sys.argv[2]
dashboard=json.loads(path.read_text(encoding='utf-8'))
assert dashboard.get('uid') == expected
assert 'V7' in str(dashboard.get('title',''))
PY

mkdir -p "$STATE_DIR/grafana/provisioning/datasources" "$STATE_DIR/grafana/provisioning/dashboards"
install -m 0644 "$APP_DIR/$DATASOURCE_FILE" "$STATE_DIR/grafana/provisioning/datasources/prometheus-v7.yml"

python3 - "$APP_DIR/$PROVIDER_FILE" "$STATE_DIR/grafana/provisioning/dashboards/v7.yml" "$APP_DIR/monitoring/grafana/dashboards" <<'PY'
import sys
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8'); replacement=sys.argv[3]; marker='__POLYMARKET_V7_DASHBOARD_DIR__'
assert source.count(marker) == 1
Path(sys.argv[2]).write_text(source.replace(marker, replacement), encoding='utf-8')
PY

install -m 0644 "$APP_DIR/$ALERT_RULES_FILE" "$STATE_DIR/prometheus-v7-alerts.yml"
python3 - "$APP_DIR/$PROMETHEUS_FILE" "$STATE_DIR/prometheus-v7.yml" "$STATE_DIR/prometheus-v7-alerts.yml" <<'PY'
import sys
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8'); marker='__POLYMARKET_V7_ALERT_RULES__'
assert source.count(marker) == 1
Path(sys.argv[2]).write_text(source.replace(marker, sys.argv[3]), encoding='utf-8')
PY

configure_tailnet_grafana

printf 'v7_monitoring_configured=true\n'
printf 'dashboard_uid=%s\n' "$DASHBOARD_UID"
printf 'dashboard_file=%s\n' "$APP_DIR/$DASHBOARD_FILE"
printf 'prometheus_config=%s\n' "$STATE_DIR/prometheus-v7.yml"
printf 'prometheus_alert_rules=%s\n' "$STATE_DIR/prometheus-v7-alerts.yml"
printf 'grafana_operator_url=%s/d/%s/polymarket-v7-canonical-paper-economics\n' "$GRAFANA_URL" "$DASHBOARD_UID"
