#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
REPO_URL="${POLYMARKET_REPO_URL:-https://github.com/ENRICOBIGNOZZI/Polymarket.git}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
APP_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
RUNTIME_DIR="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
RUNTIME_CURRENT="$RUNTIME_ROOT/current"
ARTIFACT_ROOT="${POLYMARKET_ARTIFACT_ROOT:-/home/$SERVICE_USER/polymarket-artifacts}"
RUN_ROOT="${PM_V7_RUN_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london}"
INSTALL_TAILSCALE="${POLYMARKET_INSTALL_TAILSCALE:-1}"
INSTALL_GRAFANA="${POLYMARKET_INSTALL_GRAFANA:-1}"
REUSE_EXACT_SHA_CI="${POLYMARKET_REUSE_EXACT_SHA_CI:-0}"
CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact lowercase 40-char SHA required" >&2; exit 78; }
[[ "$(uname -s)" == "Linux" ]] || { echo "Linux required" >&2; exit 78; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || {
  echo "Ubuntu 24.04 required" >&2; exit 78;
}

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential ca-certificates cmake curl ethtool git gnupg jq libboost-all-dev \
  libcurl4-openssl-dev libssl-dev libsecp256k1-dev ninja-build pkg-config \
  prometheus prometheus-node-exporter python3 python3-numpy python3-pip python3-venv rsync util-linux


if [[ "$INSTALL_GRAFANA" == 1 ]]; then
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://apt.grafana.com/gpg-full.key | sudo tee /etc/apt/keyrings/grafana.asc >/dev/null
  sudo chmod 0644 /etc/apt/keyrings/grafana.asc
  echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" | \
    sudo tee /etc/apt/sources.list.d/grafana.list >/dev/null
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y grafana
fi


if [[ "$INSTALL_TAILSCALE" == 1 ]]; then
  sudo install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg | \
    sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list | \
    sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tailscale
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --create-home --shell /bin/bash "$SERVICE_USER"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$(dirname "$APP_DIR")" "$RUN_ROOT" "$RUNTIME_ROOT/by-sha" "$ARTIFACT_ROOT/by-sha" "$(dirname "$RUN_ROOT")/paper_v7_london_archives"

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$APP_DIR"
fi
sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch --no-tags origin main
sudo -u "$SERVICE_USER" git -C "$APP_DIR" cat-file -e "$EXPECTED_SHA^{commit}"
sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout --detach "$EXPECTED_SHA"
[[ "$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]]
[[ -z "$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" status --porcelain)" ]]

# Stage the exact release without starting any runtime.
sudo -u "$SERVICE_USER" env POLYMARKET_EXPECTED_SHA="$EXPECTED_SHA" POLYMARKET_SERVICE_USER="$SERVICE_USER" \
  POLYMARKET_APP_DIR="$APP_DIR" POLYMARKET_RUNTIME_ROOT="$RUNTIME_ROOT" \
  POLYMARKET_REUSE_EXACT_SHA_CI="$REUSE_EXACT_SHA_CI" PM_V7_CI_REPOSITORY="$CI_REPOSITORY" \
  bash "$APP_DIR/ops/v7_london_stage_release.sh"
sudo -u "$SERVICE_USER" ln -sfn "by-sha/$EXPECTED_SHA" "$RUNTIME_CURRENT"
[[ "$(cat "$RUNTIME_CURRENT/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]]
[[ ! -e "$RUNTIME_CURRENT/research" ]]

render_unit() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$SERVICE_USER" "$SERVICE_GROUP" "$RUNTIME_CURRENT" "$RUN_ROOT" "$EXPECTED_SHA" <<'PY'
import os,sys
from pathlib import Path
source,destination,user,group,app,run_root,sha=sys.argv[1:]
payload=Path(source).read_text(encoding='utf-8')
for marker,value in {
    '@SERVICE_USER@':user, '@SERVICE_GROUP@':group, '@APP_DIR@':app,
    '@RUN_ROOT@':run_root, '@EXPECTED_SHA@':sha,
    '@ARCHIVE_ROOT@':str(Path(run_root).resolve().parent/'paper_v7_london_archives'),
}.items():
    payload=payload.replace(marker,value)
if '@' in payload:
    raise SystemExit(f'unrendered systemd marker in {source}')
tmp=Path(destination + f'.tmp.{os.getpid()}')
tmp.write_text(payload,encoding='utf-8')
os.chmod(tmp,0o644)
os.replace(tmp,destination)
PY
}

for name in polymarket-v7-paper.service polymarket-v7-exporter.service polymarket-v7-retention.service polymarket-v7-retention.timer; do
  tmp="$(mktemp)"
  render_unit "$RUNTIME_CURRENT/ops/systemd/$name.in" "$tmp"
  sudo install -m 0644 "$tmp" "/etc/systemd/system/$name"
  rm -f "$tmp"
done

# Cold-plane monitoring is rendered from the immutable release. The installer
# writes only monitoring files/systemd drop-ins; it never starts trading.
sudo env POLYMARKET_EXPECTED_SHA="$EXPECTED_SHA" POLYMARKET_SERVICE_USER="$SERVICE_USER" \
  POLYMARKET_RUNTIME_ROOT="$RUNTIME_ROOT" \
  bash "$RUNTIME_CURRENT/ops/v7_london_install_monitoring.sh"
sudo systemctl daemon-reload
sudo systemctl enable --now prometheus.service prometheus-node-exporter.service grafana-server.service
sudo systemctl disable --now polymarket-v7-paper.service polymarket-v7-exporter.service polymarket-v7-retention.timer >/dev/null 2>&1 || true

receipt="$RUN_ROOT/bootstrap_receipt.json"
sudo -u "$SERVICE_USER" python3 - "$receipt" "$EXPECTED_SHA" "$RUNTIME_CURRENT" <<'PY'
import json,platform,socket,sys,time
from pathlib import Path
path,sha,app=sys.argv[1:]
value={
  'schema':'polymarket_v7_london_bootstrap_receipt_v1',
  'timestamp':int(time.time()), 'hostname':socket.gethostname(),
  'platform':platform.platform(), 'code_sha':sha, 'runtime_dir':app,
  'paper_only':True, 'authenticated_execution':False,
  'real_order_submission':False, 'systemd_installed_but_disabled':True,
  'tailscale_authenticated':False,
}
Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
PY

echo "London host bootstrap complete for exact SHA $EXPECTED_SHA"
echo "Runtime remains disabled. Tailscale is installed only; authenticate it separately for admin access."
