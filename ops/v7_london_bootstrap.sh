#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
REPO_URL="${POLYMARKET_REPO_URL:-https://github.com/ENRICOBIGNOZZI/Polymarket.git}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
APP_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUN_ROOT="${PM_V7_RUN_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london}"
INSTALL_GRAFANA="${POLYMARKET_INSTALL_GRAFANA:-1}"
INSTALL_TAILSCALE="${POLYMARKET_INSTALL_TAILSCALE:-1}"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact lowercase 40-char SHA required" >&2; exit 78; }
[[ "$(uname -s)" == "Linux" ]] || { echo "Linux required" >&2; exit 78; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || {
  echo "Ubuntu 24.04 required" >&2; exit 78;
}

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential ca-certificates cmake curl git gnupg jq libboost-all-dev \
  libcurl4-openssl-dev libssl-dev ninja-build pkg-config prometheus \
  python3 python3-pip python3-venv rsync

if [[ "$INSTALL_GRAFANA" == 1 ]]; then
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor | \
    sudo tee /etc/apt/keyrings/grafana.gpg >/dev/null
  echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' | \
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
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$(dirname "$APP_DIR")" "$RUN_ROOT"

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$APP_DIR"
fi
sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch --no-tags origin main
sudo -u "$SERVICE_USER" git -C "$APP_DIR" cat-file -e "$EXPECTED_SHA^{commit}"
sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout --detach "$EXPECTED_SHA"
[[ "$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]]
[[ -z "$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" status --porcelain)" ]]

sudo -u "$SERVICE_USER" cmake -S "$APP_DIR" -B "$APP_DIR/build" -GNinja -DCMAKE_BUILD_TYPE=Release
sudo -u "$SERVICE_USER" cmake --build "$APP_DIR/build" --parallel "${POLYMARKET_BUILD_JOBS:-2}"
sudo -u "$SERVICE_USER" ctest --test-dir "$APP_DIR/build" --output-on-failure

render_unit() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$SERVICE_USER" "$SERVICE_GROUP" "$APP_DIR" "$RUN_ROOT" "$EXPECTED_SHA" <<'PY'
import os,sys
from pathlib import Path
source,destination,user,group,app,run_root,sha=sys.argv[1:]
payload=Path(source).read_text(encoding='utf-8')
for marker,value in {
    '@SERVICE_USER@':user, '@SERVICE_GROUP@':group, '@APP_DIR@':app,
    '@RUN_ROOT@':run_root, '@EXPECTED_SHA@':sha,
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
  render_unit "$APP_DIR/ops/systemd/$name.in" "$tmp"
  sudo install -m 0644 "$tmp" "/etc/systemd/system/$name"
  rm -f "$tmp"
done
sudo systemctl daemon-reload
sudo systemctl disable --now polymarket-v7-paper.service polymarket-v7-exporter.service polymarket-v7-retention.timer >/dev/null 2>&1 || true

receipt="$RUN_ROOT/bootstrap_receipt.json"
sudo -u "$SERVICE_USER" python3 - "$receipt" "$EXPECTED_SHA" "$APP_DIR" <<'PY'
import json,platform,socket,sys,time
from pathlib import Path
path,sha,app=sys.argv[1:]
value={
  'schema':'polymarket_v7_london_bootstrap_receipt_v1',
  'timestamp':int(time.time()), 'hostname':socket.gethostname(),
  'platform':platform.platform(), 'code_sha':sha, 'app_dir':app,
  'paper_only':True, 'authenticated_execution':False,
  'real_order_submission':False, 'systemd_installed_but_disabled':True,
  'tailscale_authenticated':False,
}
Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
PY

echo "London host bootstrap complete for exact SHA $EXPECTED_SHA"
echo "Runtime remains disabled. Tailscale is installed only; authenticate it separately for admin access."
