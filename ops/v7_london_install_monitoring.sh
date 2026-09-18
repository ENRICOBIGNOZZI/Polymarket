#!/usr/bin/env bash
set -euo pipefail
umask 022

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
RUNTIME_DIR="${POLYMARKET_RUNTIME_DIR:-$RUNTIME_ROOT/by-sha/$EXPECTED_SHA}"
MONITORING_ROOT="${POLYMARKET_MONITORING_ROOT:-/var/lib/polymarket-v7-monitoring}"
SYSTEMD_ROOT="${POLYMARKET_SYSTEMD_ROOT:-/etc/systemd/system}"
SYSTEMCTL="${POLYMARKET_SYSTEMCTL:-systemctl}"
TARGET="$MONITORING_ROOT/by-sha/$EXPECTED_SHA"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "Linux monitoring install required" >&2; exit 78; }
[[ "$(id -u)" == 0 ]] || { echo "root required to install monitoring control plane" >&2; exit 77; }
[[ "$SYSTEMD_ROOT" == /* ]] || { echo "absolute systemd root required" >&2; exit 78; }
command -v "$SYSTEMCTL" >/dev/null || { echo "systemctl command missing" >&2; exit 69; }
[[ -f "$RUNTIME_DIR/deploy/london/runtime_sha" ]] || { echo "staged runtime missing" >&2; exit 66; }
[[ "$(cat "$RUNTIME_DIR/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]] || { echo "staged runtime SHA mismatch" >&2; exit 66; }
[[ -f "$RUNTIME_DIR/ops/v7_london_monitoring_bundle.py" ]] || { echo "monitoring bundler missing" >&2; exit 66; }
command -v prometheus >/dev/null || { echo "prometheus package missing" >&2; exit 69; }
command -v grafana-server >/dev/null || { echo "grafana package missing" >&2; exit 69; }

install -d -m 0755 "$MONITORING_ROOT/by-sha"
tmp="$TARGET.tmp.$$"
rm -rf "$tmp"
cleanup(){ rm -rf "$tmp"; }
trap cleanup EXIT
python3 "$RUNTIME_DIR/ops/v7_london_monitoring_bundle.py" \
  --repository-root "$RUNTIME_DIR" --output "$tmp" \
  --deployed-directory "$TARGET" --expected-sha "$EXPECTED_SHA" >/dev/null

if [[ -e "$TARGET" ]]; then
  diff -qr "$tmp" "$TARGET" >/dev/null || { echo "existing monitoring bundle differs for exact SHA" >&2; exit 73; }
else
  mv "$tmp" "$TARGET"
fi
chmod -R a+rX "$TARGET"
if command -v promtool >/dev/null 2>&1; then
  promtool check config "$TARGET/prometheus-v7.yml" >/dev/null
fi
ln -sfn "by-sha/$EXPECTED_SHA" "$MONITORING_ROOT/current"

install -d -m 0755 "$SYSTEMD_ROOT/prometheus.service.d" "$SYSTEMD_ROOT/grafana-server.service.d"
install -m 0644 "$TARGET/prometheus-systemd-override.conf" "$SYSTEMD_ROOT/prometheus.service.d/polymarket-v7.conf"
install -m 0644 "$TARGET/grafana-systemd-override.conf" "$SYSTEMD_ROOT/grafana-server.service.d/polymarket-v7.conf"
"$SYSTEMCTL" daemon-reload

python3 - "$TARGET/monitoring_bundle_receipt.json" "$EXPECTED_SHA" <<'PYRECEIPT'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); sha=sys.argv[2]
v=json.loads(p.read_text())
assert v['runtime_sha']==sha
assert v['paper_only'] is True and v['authenticated_execution'] is False and v['real_order_submission'] is False
assert v['services_started'] is False and v['authentication_changed'] is False
root=p.parent
for rel,digest in v['files'].items():
    assert hashlib.sha256((root/rel).read_bytes()).hexdigest()==digest
print('monitoring_install_result=staged')
print('runtime_sha='+sha)
print('monitoring_dir='+str(root))
PYRECEIPT
