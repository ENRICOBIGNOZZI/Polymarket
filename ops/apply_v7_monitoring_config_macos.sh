#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${POLYMARKET_APP_DIR:-$ROOT}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
MANIFEST="$APP_DIR/monitoring/v7_monitoring_manifest.json"

[[ "$(uname -s)" == "Darwin" ]] || { echo "fatal: macOS monitoring installer requires Darwin" >&2; exit 78; }
[[ -f "$MANIFEST" ]] || { echo "fatal: missing V7 monitoring manifest" >&2; exit 78; }

read -r DASHBOARD_FILE DATASOURCE_FILE PROVIDER_FILE PROMETHEUS_FILE ALERT_RULES_FILE DASHBOARD_UID < <(
  python3 - "$MANIFEST" <<'PY'
import json,sys
from pathlib import Path, PurePosixPath
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
assert manifest.get('schema') == 'polymarket_v7_monitoring_manifest_v1'
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

mkdir -p \
  "$STATE_DIR/grafana/provisioning/datasources" \
  "$STATE_DIR/grafana/provisioning/dashboards"

install -m 0644 "$APP_DIR/$DATASOURCE_FILE" \
  "$STATE_DIR/grafana/provisioning/datasources/prometheus-v7.yml"

python3 - "$APP_DIR/$PROVIDER_FILE" "$STATE_DIR/grafana/provisioning/dashboards/v7.yml" "$APP_DIR/monitoring/grafana/dashboards" <<'PY'
import sys
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8')
replacement=sys.argv[3]
marker='__POLYMARKET_V7_DASHBOARD_DIR__'
assert source.count(marker) == 1
Path(sys.argv[2]).write_text(source.replace(marker, replacement), encoding='utf-8')
PY

install -m 0644 "$APP_DIR/$ALERT_RULES_FILE" "$STATE_DIR/prometheus-v7-alerts.yml"
python3 - "$APP_DIR/$PROMETHEUS_FILE" "$STATE_DIR/prometheus-v7.yml" "$STATE_DIR/prometheus-v7-alerts.yml" <<'PY'
import sys
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8')
marker='__POLYMARKET_V7_ALERT_RULES__'
assert source.count(marker) == 1
Path(sys.argv[2]).write_text(source.replace(marker, sys.argv[3]), encoding='utf-8')
PY

printf 'v7_monitoring_configured=true\n'
printf 'dashboard_uid=%s\n' "$DASHBOARD_UID"
printf 'dashboard_file=%s\n' "$APP_DIR/$DASHBOARD_FILE"
printf 'prometheus_config=%s\n' "$STATE_DIR/prometheus-v7.yml"
printf 'prometheus_alert_rules=%s\n' "$STATE_DIR/prometheus-v7-alerts.yml"
