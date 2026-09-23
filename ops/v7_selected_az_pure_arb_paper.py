#!/usr/bin/env python3
"""Run one bounded exact-SHA PureArb multi-market PAPER process on one selected London host."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from v7_london_ssm_benchmark import send, wait_one

SHA_RE=re.compile(r"^[0-9a-f]{40}$")
IID_RE=re.compile(r"^i-[0-9a-f]+$")


def remote_command(sha:str, duration:int, service_user:str)->str:
    if not SHA_RE.fullmatch(sha): raise ValueError("invalid sha")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}",service_user):
        raise ValueError("invalid service user")
    if not 30 <= duration <= 1800: raise ValueError("duration out of range")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
OUT=/mnt/polymarket-data/benchmarks/selected-pure-arb-{sha}
SRC="$OUT/source"
STEP=preflight
dump_selected_failure() {{
  rc=$?
  echo "selected_pure_arb_step=$STEP rc=$rc" >&2
  for name in universe.log config.log runtime.log runtime.json; do
    if [[ -s "$OUT/$name" ]]; then
      echo "===== $name =====" >&2
      tail -c 5000 "$OUT/$name" >&2 || true
      echo >&2
    fi
  done
  sudo -u {service_user} git -C "$APP" worktree remove --force "$SRC" >/dev/null 2>&1 || true
  exit "$rc"
}}
trap dump_selected_failure ERR
sudo systemctl stop polymarket-v7-paper.service >/dev/null 2>&1 || true
! systemctl is-active --quiet polymarket-v7-paper.service
rm -rf "$OUT"
install -d -o {service_user} -g "$(id -gn {service_user})" "$OUT"
STEP=source_worktree
sudo -u {service_user} git -C "$APP" cat-file -e "{sha}^{{commit}}" 2>/dev/null \
  || sudo -u {service_user} git -C "$APP" fetch --no-tags origin "{sha}"
sudo -u {service_user} git -C "$APP" worktree remove --force "$SRC" >/dev/null 2>&1 || true
rm -rf "$SRC"
sudo -u {service_user} git -C "$APP" worktree prune
sudo -u {service_user} git -C "$APP" worktree add --detach "$SRC" "{sha}" >/dev/null
[[ "$(sudo -u {service_user} git -C "$SRC" rev-parse HEAD)" == "{sha}" ]]
cd "$SRC"
STEP=capital_allocator
sudo -u {service_user} python3 "$SRC/scripts/v7_capital_allocator.py"   --config "$SRC/config/paper_v7.json" --output-dir "$OUT/alloc" > "$OUT/allocator.log"
STEP=universe
sudo -u {service_user} python3 "$SRC/scripts/v7_crypto_universe.py"   --config "$SRC/config/v7_crypto_universe.json" --output-dir "$OUT/universe"   --model-sha "{sha}" --once > "$OUT/universe.log"
STEP=runtime_config
sudo -u {service_user} python3 "$SRC/scripts/v7_pure_arb_multi_config.py"   --universe "$OUT/universe/current.json"   --allocation "$OUT/alloc/crypto_settlement_engine.json"   --model-sha "{sha}" --latency-tape "$OUT/native-latency.bin"   --output "$OUT/runtime.json" > "$OUT/config.log"
BUILD="$OUT/build"
STEP=cmake_configure
sudo -u {service_user} cmake -S "$SRC" -B "$BUILD" -G Ninja   -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DPM_LONDON_RUNTIME_ONLY=ON   > "$OUT/cmake-configure.log"
STEP=cmake_build
sudo -u {service_user} cmake --build "$BUILD" --parallel "$(nproc)"   --target polymarket_v7_pure_arb_multi_runtime > "$OUT/cmake-build.log"
STEP=runtime
set +e
sudo -u {service_user} "$BUILD/polymarket_v7_pure_arb_multi_runtime"   --config "$OUT/runtime.json" --duration-seconds "{duration}"   > "$OUT/runtime.log" 2>&1
rc=$?
set -e
if (( rc != 0 )); then
  false
fi
trap - ERR
python3 - "$OUT/runtime.log" "$OUT/runtime.json" "$rc" <<'PY'
import json,sys
from pathlib import Path
log=Path(sys.argv[1]); config=Path(sys.argv[2]); rc=int(sys.argv[3])
lines=[x for x in log.read_text(errors='replace').splitlines() if x.strip()]
summary=None
for line in reversed(lines):
    try: value=json.loads(line)
    except Exception: continue
    if value.get('schema')=='polymarket_v7_pure_arb_multi_runtime_v1':
        summary=value; break
if summary is None: raise SystemExit('pure arb runtime summary missing')
assert rc==0, (rc,summary)
assert summary['paper_only'] is True
assert summary['authenticated_execution'] is False
assert summary['real_order_submission'] is False
assert summary['real_capital_at_risk'] is False
assert summary['single_process'] is True
assert summary['pm_worker_count']==1
assert summary['direct_decision_queue_depth']==0
assert summary['one_leg_fills']==0
assert summary['invalid_pairs']==0
assert summary['latency_dropped']==0
assert summary['latency_queue_depth']==0
assert summary['clean'] is True
cfg=json.load(config.open())
assert len(cfg['markets'])==summary['market_count'] and summary['market_count']>0
compact={{
 'schema':'polymarket_v7_selected_az_pure_arb_validation_v1',
 'target_sha':'{sha}','paper_only':True,'authenticated_execution':False,
 'real_order_submission':False,'single_process':True,
 'pm_worker_count':summary['pm_worker_count'],'market_count':summary['market_count'],
 'frames':summary['frames'],'decoded_events':summary['decoded_events'],
 'evaluations':summary['evaluations'],'pair_admissions':summary['pair_admissions'],
 'paired_fills':summary['paired_fills'],'one_leg_fills':summary['one_leg_fills'],
 'invalid_pairs':summary['invalid_pairs'],'lineage_faults':summary['lineage_faults'],
 'pm_reconnects':summary['pm_reconnects'],'pm_errors':summary['pm_errors'],
 'latency_dropped':summary['latency_dropped'],'clean':summary['clean'],
 'duration_seconds':{duration},
}}
Path(log.parent/'selected-summary.json').write_text(json.dumps(compact,sort_keys=True,indent=2)+'\\n')
print('V7_SELECTED_PURE_ARB='+json.dumps(compact,sort_keys=True,separators=(',',':')))
PY
STEP=cleanup
sudo -u {service_user} git -C "$APP" worktree remove --force "$SRC" >/dev/null
trap - ERR'''


def parse(stdout:str)->dict[str,Any]:
    rows=[x[len("V7_SELECTED_PURE_ARB="):] for x in stdout.splitlines()
          if x.startswith("V7_SELECTED_PURE_ARB=")]
    if len(rows)!=1: raise RuntimeError("selected pure arb marker missing")
    value=json.loads(rows[0])
    if value.get("schema")!="polymarket_v7_selected_az_pure_arb_validation_v1":
        raise RuntimeError("selected pure arb schema invalid")
    return value


def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--region",default="eu-west-2")
    p.add_argument("--service-user",default="ubuntu")
    p.add_argument("--duration-seconds",type=int,default=180)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    if a.region!="eu-west-2" or not SHA_RE.fullmatch(a.expected_sha):
        raise SystemExit("eu-west-2 exact SHA required")
    if not IID_RE.fullmatch(a.instance_id): raise SystemExit("instance id required")
    timeout=max(900,a.duration_seconds+600)
    cid=send(a.region,a.instance_id,
             remote_command(a.expected_sha,a.duration_seconds,a.service_user),timeout)
    result=wait_one(a.region,cid,a.instance_id,time.monotonic()+timeout+300,5.0)
    if result.get("Status")!="Success":
        raise RuntimeError(
            f"selected PureArb PAPER failed command={cid}: "
            f"{str(result.get('StandardErrorContent') or '')[-4000:]}")
    value=parse(str(result.get("StandardOutputContent") or ""))
    receipt={
      "schema":"polymarket_v7_selected_az_pure_arb_ssm_receipt_v1",
      "timestamp":int(time.time()),"region":a.region,
      "instance_id":a.instance_id,"command_id":cid,
      "expected_sha":a.expected_sha,"paper_only":True,
      "authenticated_execution":False,"real_order_submission":False,
      "automatic_cutover":False,"runtime":value,
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
    print(f"receipt={a.output}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
