#!/usr/bin/env python3
"""Install or refresh the zero-authority London collection plane through SSM.

This path is deliberately separate from the trading-model cutover transport.
It never stops, restarts, deploys, promotes, or reconfigures
polymarket-v7-paper.service.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import time

from v7_london_ssm_deploy import (
    REGION,
    SsmDeployError,
    aws_json,
    wait,
)

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
COMMENT = "Polymarket V7 model-independent collection plane"


def send(region: str, instance: str, command: str, timeout_s: int) -> str:
    if not INSTANCE_RE.fullmatch(instance):
        raise SsmDeployError("invalid collection instance id")
    parameters = json.dumps({
        "commands": [f"bash -lc {shlex.quote(command)}"],
        "executionTimeout": [str(timeout_s)],
    })
    value = aws_json(region, [
        "ssm", "send-command",
        "--instance-ids", instance,
        "--document-name", "AWS-RunShellScript",
        "--parameters", parameters,
        "--comment", COMMENT,
    ])
    command_id = (value.get("Command") or {}).get("CommandId")
    if not isinstance(command_id, str) or not command_id:
        raise SsmDeployError("collection SSM command id missing")
    return command_id


def run(region: str, instance: str, command: str, timeout_s: int) -> tuple[str, str]:
    command_id = send(region, instance, command, timeout_s)
    value = wait(region, instance, command_id, timeout_s + 120, 2.0)
    stdout = str(value.get("StandardOutputContent") or "")
    stderr = str(value.get("StandardErrorContent") or "")
    if value.get("Status") != "Success":
        status = str(value.get("StatusDetails") or value.get("Status") or "")
        raise SsmDeployError(
            "collection SSM failed "
            f"instance={instance} command={command_id} "
            f"stdout_tail={stdout[-8000:]!r} stderr_tail={stderr[-4000:]!r} "
            f"status={status!r}"
        )
    return stdout, stderr


def install_command(sha: str) -> str:
    if not SHA_RE.fullmatch(sha):
        raise ValueError("exact collection SHA required")
    return f"""set -euo pipefail
SHA={sha}
PAPER_UNIT=polymarket-v7-paper.service
COLLECTION_UNIT=polymarket-v7-collection.service
RETENTION_UNIT=polymarket-v7-collection-retention.service
RETENTION_TIMER=polymarket-v7-collection-retention.timer

SERVICE_USER="$(systemctl show "$PAPER_UNIT" -p User --value)"
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$SERVICE_USER" != root ]]
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
APP="/home/$SERVICE_USER/polymarket"
[[ -d "$APP/.git" ]]
RUNTIME_ROOT="/home/$SERVICE_USER/polymarket-collection-runtime"
TARGET="$RUNTIME_ROOT/by-sha/$SHA"
CURRENT="$RUNTIME_ROOT/current"
COLLECTION_ROOT="/mnt/polymarket-data/polymarket_v7_collection"
ARCHIVE_ROOT="/mnt/polymarket-data/polymarket_v7_collection_archives"

# Prove that this operation never controls the trading model service.
PAPER_PID_BEFORE="$(systemctl show "$PAPER_UNIT" -p MainPID --value)"
PAPER_ACTIVE_BEFORE="$(systemctl is-active "$PAPER_UNIT" || true)"

sudo -u "$SERVICE_USER" git -C "$APP" fetch --no-tags origin main
sudo -u "$SERVICE_USER" git -C "$APP" cat-file -e "$SHA^{{commit}}"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$RUNTIME_ROOT" "$RUNTIME_ROOT/by-sha"

if [[ -e "$TARGET" ]]; then
  sudo systemctl stop "$COLLECTION_UNIT" >/dev/null 2>&1 || true
  sudo -u "$SERVICE_USER" git -C "$APP" worktree remove --force "$TARGET" >/dev/null 2>&1 || true
  rm -rf -- "$TARGET"
fi
sudo -u "$SERVICE_USER" git -C "$APP" worktree prune
sudo -u "$SERVICE_USER" git -C "$APP" worktree add --detach "$TARGET" "$SHA" >/dev/null
[[ "$(sudo -u "$SERVICE_USER" git -C "$TARGET" rev-parse HEAD)" == "$SHA" ]]
[[ -z "$(sudo -u "$SERVICE_USER" git -C "$TARGET" status --porcelain)" ]]

command -v cmake >/dev/null
command -v ninja >/dev/null
command -v g++ >/dev/null
sudo -u "$SERVICE_USER" cmake -S "$TARGET" -B "$TARGET/build" -G Ninja   -DCMAKE_BUILD_TYPE=Release -DPM_LONDON_RUNTIME_ONLY=ON -DBUILD_TESTING=OFF >/dev/null
sudo -u "$SERVICE_USER" nice -n 10 cmake --build "$TARGET/build" --parallel 2 --target   polymarket_v7_trade_recorder   polymarket_v7_maker_fillability_observer   polymarket_v7_external_venue_runtime

bash -n "$TARGET/scripts/v7_collection_plane.sh"
python3 -m py_compile   "$TARGET/scripts/v7_crypto_universe.py"   "$TARGET/scripts/v7_multi_asset_external_collector.py"   "$TARGET/scripts/v7_rtds_external_fair_monitor.py"   "$TARGET/monitoring/v7_london_buffer_retention.py"

install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$COLLECTION_ROOT" "$ARCHIVE_ROOT"
ln -sfn "by-sha/$SHA" "$CURRENT"
chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT"

render_unit() {{
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$SERVICE_USER" "$SERVICE_GROUP" "$CURRENT"     "$COLLECTION_ROOT" "$ARCHIVE_ROOT" "$SHA" <<'PY'
import os,sys
from pathlib import Path
source,destination,user,group,app,root,archive,sha=sys.argv[1:]
payload=Path(source).read_text(encoding='utf-8')
values={{
 '@SERVICE_USER@':user,
 '@SERVICE_GROUP@':group,
 '@APP_DIR@':app,
 '@COLLECTION_ROOT@':root,
 '@COLLECTION_ARCHIVE_ROOT@':archive,
 '@EXPECTED_SHA@':sha,
}}
for key,value in values.items():
    payload=payload.replace(key,value)
if '@' in payload:
    raise SystemExit('unrendered collection systemd marker')
target=Path(destination)
temporary=target.with_name(target.name+'.tmp')
temporary.write_text(payload,encoding='utf-8')
os.chmod(temporary,0o644)
os.replace(temporary,target)
PY
}}

render_unit "$TARGET/ops/systemd/polymarket-v7-collection.service.in"   "/etc/systemd/system/$COLLECTION_UNIT"
render_unit "$TARGET/ops/systemd/polymarket-v7-collection-retention.service.in"   "/etc/systemd/system/$RETENTION_UNIT"
render_unit "$TARGET/ops/systemd/polymarket-v7-collection-retention.timer.in"   "/etc/systemd/system/$RETENTION_TIMER"

systemctl daemon-reload
systemctl enable "$COLLECTION_UNIT" "$RETENTION_TIMER" >/dev/null
# A prior fail-closed launch may have exhausted StartLimitBurst. Reset only the
# independent collection unit's failed/start-limit state; never touch PAPER.
systemctl reset-failed "$COLLECTION_UNIT" >/dev/null 2>&1 || true
systemctl restart "$COLLECTION_UNIT"
systemctl start "$RETENTION_TIMER"

ready=0
for _ in $(seq 1 240); do
  if systemctl is-active --quiet "$COLLECTION_UNIT" &&      python3 - "$COLLECTION_ROOT" "$SHA" <<'PY' >/dev/null 2>&1
import json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
runtime=json.loads((root/'control/runtime_status.json').read_text())
universe=json.loads((root/'universe/status.json').read_text())
external=json.loads((root/'external_fair/all_assets_status.json').read_text())
book=json.loads((root/'research/repricing_book/fillability_ws_status.json').read_text())
assert runtime.get('schema')=='polymarket_v7_collection_plane_status_v1'
assert runtime.get('collector_sha')==sha
assert runtime.get('model_independent') is True
assert runtime.get('live_model_required') is False
assert runtime.get('execution_authority')=='ZERO_AUTHORITY_DATA_COLLECTION'
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
assert runtime.get('state')=='COLLECTING'
assert time.time_ns()-int(runtime.get('timestamp_ns') or 0) < 10_000_000_000
children=runtime.get('children') or []
assert len(children)==6 and all(row.get('alive') is True for row in children)
assert universe.get('model_sha')==sha and universe.get('state')=='OPERATIONAL'
assert int(universe.get('book_selection_contexts') or 0)==30
assert int(universe.get('book_selection_tokens') or 0)==60
assert external.get('model_sha')==sha and external.get('state')=='OPERATIONAL'
assert int(external.get('ready_assets') or 0)==6
assert book.get('model_sha')==sha and book.get('state')=='running'
assert int(book.get('observed_tokens') or 0)>0
assert int(book.get('dropped_events') or 0)==0
assert int(book.get('decoder_failures') or 0)==0
PY
  then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]]

before="$(python3 - "$COLLECTION_ROOT" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
paths=[]
for pattern in (
    'external_fair/raw/*',
    'external_fair/assets/*/raw/*',
    'external_fair/normalized_events/*',
    'external_fair/assets/*/normalized_events/*',
    'research/repricing_book/book_observations/*',
):
    paths.extend(root.glob(pattern))
print(sum(p.stat().st_size for p in paths if p.is_file() and not p.is_symlink()))
PY
)"
sleep 10
after="$(python3 - "$COLLECTION_ROOT" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
paths=[]
for pattern in (
    'external_fair/raw/*',
    'external_fair/assets/*/raw/*',
    'external_fair/normalized_events/*',
    'external_fair/assets/*/normalized_events/*',
    'research/repricing_book/book_observations/*',
):
    paths.extend(root.glob(pattern))
print(sum(p.stat().st_size for p in paths if p.is_file() and not p.is_symlink()))
PY
)"
[[ "$before" =~ ^[0-9]+$ && "$after" =~ ^[0-9]+$ && "$after" -gt "$before" ]]
GROWTH_BYTES=$((after-before))

PAPER_PID_AFTER="$(systemctl show "$PAPER_UNIT" -p MainPID --value)"
PAPER_ACTIVE_AFTER="$(systemctl is-active "$PAPER_UNIT" || true)"
[[ "$PAPER_PID_AFTER" == "$PAPER_PID_BEFORE" ]]
[[ "$PAPER_ACTIVE_AFTER" == "$PAPER_ACTIVE_BEFORE" ]]

python3 - "$SHA" "$COLLECTION_ROOT" "$GROWTH_BYTES" "$PAPER_PID_AFTER" <<'PY'
import json,sys
print('V7_COLLECTION_READY='+json.dumps({{
 'schema':'polymarket_v7_collection_plane_install_receipt_v1',
 'collector_sha':sys.argv[1],
 'collection_root':sys.argv[2],
 'growth_bytes_10s':int(sys.argv[3]),
 'paper_service_pid_unchanged':True,
 'paper_service_pid':int(sys.argv[4] or 0),
 'model_independent':True,
 'paper_only':True,
 'authenticated_execution':False,
 'real_order_submission':False,
 'real_capital_at_risk':False,
 'execution_authority':'ZERO_AUTHORITY_DATA_COLLECTION',
}},sort_keys=True,separators=(',',':')))
PY
"""


def parse_marker(stdout: str) -> dict:
    prefix = "V7_COLLECTION_READY="
    rows = [line for line in stdout.splitlines() if line.startswith(prefix)]
    if len(rows) != 1:
        raise SsmDeployError("collection ready receipt missing")
    value = json.loads(rows[0][len(prefix):])
    if (
        value.get("schema") != "polymarket_v7_collection_plane_install_receipt_v1"
        or value.get("model_independent") is not True
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("execution_authority") != "ZERO_AUTHORITY_DATA_COLLECTION"
        or value.get("paper_service_pid_unchanged") is not True
        or int(value.get("growth_bytes_10s") or 0) <= 0
    ):
        raise SsmDeployError("collection ready receipt contract invalid")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.region != REGION:
        parser.error("collection plane must remain eu-west-2")
    if not SHA_RE.fullmatch(args.expected_sha):
        parser.error("exact expected SHA required")
    if not INSTANCE_RE.fullmatch(args.instance_id):
        parser.error("valid instance id required")

    # Read-only identity proof before the collection-only command.
    aws_json(args.region, ["sts", "get-caller-identity"])
    try:
        stdout, stderr = run(
            args.region,
            args.instance_id,
            install_command(args.expected_sha),
            2400,
        )
        receipt = parse_marker(stdout)
    except (OSError, ValueError, json.JSONDecodeError, SsmDeployError) as exc:
        parser.exit(2, f"v7_collection_plane_ssm: {exc}\n")

    receipt["instance_id"] = args.instance_id
    receipt["region"] = args.region
    receipt["stderr_tail"] = stderr[-2000:]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("collection_plane_result=success")
    print("collection_plane_growth_bytes_10s=" + str(receipt["growth_bytes_10s"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
