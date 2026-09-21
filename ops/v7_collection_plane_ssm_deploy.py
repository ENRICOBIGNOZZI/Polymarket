#!/usr/bin/env python3
"""Deploy only the model-independent London collection plane through SSM.

This operation never stops, restarts, deploys, or mutates the PAPER trading
runtime. It installs a separate immutable collector worktree + binaries and
systemd units that write to their own stable collection root.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex

from v7_london_ssm_deploy import REGION, SsmDeployError, run

INSTANCE = "i-0fba2bac9fdc5cbeb"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def remote_command(expected_sha: str) -> str:
    if not SHA_RE.fullmatch(expected_sha):
        raise ValueError("exact collector SHA required")
    return f"""set -euo pipefail
SHA={expected_sha}
PAPER_UNIT=polymarket-v7-paper.service
SERVICE_USER="$(systemctl show "$PAPER_UNIT" -p User --value)"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
MODEL_APP="$(systemctl show "$PAPER_UNIT" -p WorkingDirectory --value)"
MODEL_ENV="$(systemctl show "$PAPER_UNIT" -p Environment --value)"
[[ -n "$SERVICE_USER" && "$SERVICE_USER" != root ]]
[[ "$MODEL_APP" == "/home/$SERVICE_USER/polymarket-runtime/current" || "$MODEL_APP" == "/home/$SERVICE_USER/polymarket" || "$MODEL_APP" == /home/"$SERVICE_USER"/polymarket-runtime/by-sha/* ]]

MODEL_RUN_ROOT="$(python3 - "$MODEL_ENV" <<'PY'
import shlex,sys
items=shlex.split(sys.argv[1])
print(next((item.split('=',1)[1] for item in items if item.startswith('PM_V7_RUN_ROOT=')),'')) 
PY
)"
[[ "$MODEL_RUN_ROOT" == /* ]]
python3 - "$MODEL_RUN_ROOT/control/runtime_status.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1],encoding='utf-8'))
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
PY

SOURCE_REPO="/home/$SERVICE_USER/polymarket"
COLLECTION_BASE="/home/$SERVICE_USER/polymarket-collection"
WORKTREE="$COLLECTION_BASE/by-sha/$SHA"
BUILD="$COLLECTION_BASE/build/$SHA"
COLLECTION_ROOT="/mnt/polymarket-data/polymarket_v7_collection"
COLLECTION_ARCHIVE_ROOT="/mnt/polymarket-data/polymarket_v7_collection_archives"

[[ -d "$SOURCE_REPO/.git" ]]
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$COLLECTION_BASE" "$COLLECTION_BASE/by-sha" "$COLLECTION_BASE/build"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$COLLECTION_ROOT" "$COLLECTION_ARCHIVE_ROOT"

sudo -u "$SERVICE_USER" git -C "$SOURCE_REPO" fetch --no-tags origin main
sudo -u "$SERVICE_USER" git -C "$SOURCE_REPO" cat-file -e "$SHA^{{commit}}"
if [[ ! -d "$WORKTREE/.git" && ! -f "$WORKTREE/.git" ]]; then
  rm -rf "$WORKTREE"
  sudo -u "$SERVICE_USER" git -C "$SOURCE_REPO" worktree prune
  sudo -u "$SERVICE_USER" git -C "$SOURCE_REPO" worktree add --detach "$WORKTREE" "$SHA" >/dev/null
fi
[[ "$(sudo -u "$SERVICE_USER" git -C "$WORKTREE" rev-parse HEAD)" == "$SHA" ]]
[[ -z "$(sudo -u "$SERVICE_USER" git -C "$WORKTREE" status --porcelain)" ]]

sudo -u "$SERVICE_USER" cmake -S "$WORKTREE" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release -DPM_LONDON_RUNTIME_ONLY=ON -DBUILD_TESTING=OFF >/dev/null
sudo -u "$SERVICE_USER" cmake --build "$BUILD" --parallel "$(nproc)" --target   polymarket_v7_trade_recorder   polymarket_v7_maker_fillability_observer   polymarket_v7_external_venue_runtime >/dev/null

for binary in polymarket_v7_trade_recorder polymarket_v7_maker_fillability_observer polymarket_v7_external_venue_runtime; do
  [[ -x "$BUILD/$binary" ]]
done

render_unit() {{
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$SERVICE_USER" "$SERVICE_GROUP" "$WORKTREE" "$COLLECTION_ROOT" "$COLLECTION_ARCHIVE_ROOT" "$SHA" <<'PY'
import os,sys
from pathlib import Path
source,destination,user,group,app,root,archive,sha=sys.argv[1:]
payload=Path(source).read_text(encoding='utf-8')
replacements={{
  '@SERVICE_USER@':user,
  '@SERVICE_GROUP@':group,
  '@APP_DIR@':app,
  '@COLLECTION_ROOT@':root,
  '@COLLECTION_ARCHIVE_ROOT@':archive,
  '@EXPECTED_SHA@':sha,
}}
for key,value in replacements.items():
    payload=payload.replace(key,value)
if '@' in payload:
    raise SystemExit('unrendered collection systemd marker')
path=Path(destination)
tmp=path.with_name(path.name+'.tmp')
tmp.write_text(payload,encoding='utf-8')
os.chmod(tmp,0o644)
os.replace(tmp,path)
PY
}}

for name in polymarket-v7-collection.service polymarket-v7-collection-retention.service polymarket-v7-collection-retention.timer; do
  tmp="$(mktemp)"
  render_unit "$WORKTREE/ops/systemd/$name.in" "$tmp"
  install -m 0644 "$tmp" "/etc/systemd/system/$name"
  rm -f "$tmp"
done

mkdir -p /etc/systemd/system/polymarket-v7-collection.service.d
cat > /etc/systemd/system/polymarket-v7-collection.service.d/binaries.conf <<EOF
[Service]
Environment=PM_V7_COLLECTION_TRADE_RECORDER=$BUILD/polymarket_v7_trade_recorder
Environment=PM_V7_COLLECTION_BOOK_OBSERVER=$BUILD/polymarket_v7_maker_fillability_observer
Environment=PM_V7_COLLECTION_EXTERNAL_RUNTIME=$BUILD/polymarket_v7_external_venue_runtime
EOF

systemctl daemon-reload
systemctl enable polymarket-v7-collection.service polymarket-v7-collection-retention.timer >/dev/null
systemctl restart polymarket-v7-collection.service
systemctl start polymarket-v7-collection-retention.timer

ready=0
for _ in $(seq 1 180); do
  if systemctl is-active --quiet polymarket-v7-collection.service &&      python3 - "$COLLECTION_ROOT/control/runtime_status.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,os,sys,time
v=json.load(open(sys.argv[1],encoding='utf-8'))
assert v.get('schema')=='polymarket_v7_collection_plane_status_v1'
assert v.get('state')=='COLLECTING'
assert v.get('collector_sha')==sys.argv[2]
assert v.get('model_independent') is True
assert v.get('live_model_required') is False
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert v.get('real_capital_at_risk') is False
assert v.get('execution_authority')=='ZERO_AUTHORITY_DATA_COLLECTION'
children=v.get('children') or []
assert len(children)==6 and all(row.get('alive') is True for row in children)
assert int(time.time())-int(v.get('timestamp') or 0)<=5
PY
  then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]]

health_json="$(sudo -u "$SERVICE_USER" python3 "$WORKTREE/monitoring/v7_hft_data_health.py" --root "$COLLECTION_ROOT" --seconds 10)"
receipt="$(python3 - "$health_json" "$SHA" "$COLLECTION_ROOT" <<'PY'
import json,sys
health=json.loads(sys.argv[1])
sha=sys.argv[2];root=sys.argv[3]
rates=health.get('gb_per_hour') or {{}}
ingress=sum(float(rates.get(key) or 0.0) for key in ('cex_raw','cex_normalized','pm_book','native_research','permanent'))
fresh=[
 row for row in (health.get('feed_quality') or [])
 if row.get('connected') is True and row.get('healthy') is True
 and row.get('stale') is False and int(row.get('events_in_sample') or 0)>0
]
assert health.get('paper_only') is True
assert health.get('authenticated_execution') is False
assert health.get('real_order_submission') is False
assert ingress > 0.0, rates
assert len(fresh) >= 2, fresh
value={{
 'schema':'polymarket_v7_independent_collection_deploy_receipt_v1',
 'collector_sha':sha,
 'collection_root':root,
 'model_independent':True,
 'live_model_required':False,
 'paper_only':True,
 'authenticated_execution':False,
 'real_order_submission':False,
 'real_capital_at_risk':False,
 'execution_authority':'ZERO_AUTHORITY_DATA_COLLECTION',
 'service_active':True,
 'retention_timer_active':True,
 'observed_ingress_gb_per_hour':ingress,
 'fresh_external_feeds':len(fresh),
 'growth_rates_gb_per_hour':rates,
}}
print(json.dumps(value,sort_keys=True,separators=(',',':')))
PY
)"
printf 'V7_COLLECTION_DEPLOY=%s\n' "$receipt"
"""


def deploy(expected_sha: str) -> dict:
    stdout, _ = run(REGION, INSTANCE, remote_command(expected_sha), 2400)
    marker = next(
        (line for line in stdout.splitlines()
         if line.startswith("V7_COLLECTION_DEPLOY=")),
        None,
    )
    if marker is None:
        raise SsmDeployError("independent collection deploy receipt missing")
    value = json.loads(marker.split("=", 1)[1])
    if value.get("collector_sha") != expected_sha:
        raise SsmDeployError("collector SHA receipt mismatch")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not SHA_RE.fullmatch(args.expected_sha):
        parser.error("exact 40-character collector SHA required")
    try:
        receipt = deploy(args.expected_sha)
    except (OSError, ValueError, SsmDeployError) as exc:
        parser.exit(2, f"v7_collection_plane_ssm_deploy: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("collection_deploy_result=success")
    print("collection_model_independent=true")
    print("collection_root=" + str(receipt["collection_root"]))
    print("collection_ingress_gb_per_hour=" + str(
        receipt["observed_ingress_gb_per_hour"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
