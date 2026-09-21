#!/usr/bin/env python3
"""Read-only exact-SHA health verification for the London PAPER runtime over AWS SSM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from v7_london_ssm_deploy import (
    REGION, STACK, SsmDeployError, aws_json, candidate_instances,
    exact_sha, parse_marker, probe, run, select_target,
)

SCHEMA = "polymarket_v7_ssm_health_receipt_v1"


def health_command(expected_sha: str) -> str:
    if not exact_sha(expected_sha):
        raise SsmDeployError("invalid health SHA")
    template = r"""set -euo pipefail
trap 'rc=$?; echo "V7_SSM_HEALTH_FAIL line=${LINENO} command=${BASH_COMMAND} rc=${rc}" >&2; exit "$rc"' ERR
SHA=__EXPECTED_SHA__
UNIT=polymarket-v7-paper.service
EXPORTER=polymarket-v7-exporter.service

echo "health_probe=services"
[[ "$(systemctl is-active "$UNIT")" == active ]]
[[ "$(systemctl is-active "$EXPORTER")" == active ]]
APP="$(systemctl show "$UNIT" -p WorkingDirectory --value)"
ENV_RAW="$(systemctl show "$UNIT" -p Environment --value)"
ROOT="$(python3 -c 'import shlex,sys; x=shlex.split(sys.stdin.read()); print(next((v.split("=",1)[1] for v in x if v.startswith("PM_V7_RUN_ROOT=")), ""))' <<<"$ENV_RAW")"
[[ -n "$APP" && -d "$APP" && -n "$ROOT" && -d "$ROOT" ]]
[[ "$(cat "$APP/deploy/london/runtime_sha")" == "$SHA" ]]
DEPLOYED_SHA_FILE="$ROOT/control/deployed_sha"
if [[ -e "$DEPLOYED_SHA_FILE" ]]; then
  [[ -f "$DEPLOYED_SHA_FILE" && ! -L "$DEPLOYED_SHA_FILE" ]]
  [[ "$(cat "$DEPLOYED_SHA_FILE")" == "$SHA" ]]
fi

echo "health_probe=runtime"
python3 - "$ROOT" "$SHA" <<'PY'
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]; now=int(time.time())
r=json.loads((root/'control/runtime_status.json').read_text())
a=json.loads((root/'control/allocations/manifest.json').read_text())
assert r.get('state')=='running', r
assert r.get('model_sha')==sha, r
assert r.get('paper_only') is True, r
assert r.get('authenticated_execution') is False, r
assert r.get('real_order_submission') is False, r
assert r.get('economic_new_risk_ready') is False, r
assert r.get('authorized_alpha_actions') in (None, []), r
assert set(r.get('economic_engines') or [])=={'CRYPTO_SETTLEMENT_ENGINE'}, r
assert a.get('engine_count')==1, a
assert set((a.get('engine_budgets') or {}).keys())=={'CRYPTO_SETTLEMENT_ENGINE'}, a
pid=int(r.get('pid') or 0); assert pid>0, r; os.kill(pid,0)
assert now-int(r.get('timestamp') or 0)<=180, r
PY

echo "health_probe=bilateral_capture"
python3 - "$ROOT" <<'PY'
import gzip,json,sys,time
from collections import Counter
from pathlib import Path

root=Path(sys.argv[1])
status=json.loads((root/'control/runtime_status.json').read_text())
hft=root/'research/hft_permanent'
now_ns=time.time_ns()
cutoff_ns=now_ns-900_000_000_000  # recent 15m only
candidates=[]
for folder in (hft/'compact', hft/'compact_closed'):
    if not folder.is_dir() or folder.is_symlink():
        continue
    for path in folder.glob('*.jsonl*'):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat=path.stat()
        except OSError:
            continue
        if stat.st_mtime_ns < cutoff_ns:
            continue
        candidates.append((stat.st_mtime_ns, stat.st_size, path))
candidates.sort(reverse=True)

max_files=96
max_bytes=128*1024*1024
selected=[]
selected_bytes=0
truncated=False
for _,size,path in candidates:
    if len(selected)>=max_files or selected_bytes+size>max_bytes:
        truncated=True
        continue
    selected.append(path)
    selected_bytes+=size

def wall_ns(row):
    if row.get('kind')==2 and isinstance(row.get('decision_wall_ns'),int):
        return row['decision_wall_ns']
    observed=row.get('observed_monotonic_ns') or row.get('decision_monotonic_ns')
    close_wall=row.get('close_wall_ns')
    close_mono=row.get('close_monotonic_ns')
    if all(isinstance(v,int) and v>0 for v in (observed,close_wall,close_mono)):
        return observed+close_wall-close_mono
    return 0

def pair_state(row):
    if row.get('repricing_pair_valid') is not True:
        return 'UNAVAILABLE'
    try:
        yb=int(row['yes_bid_e4']); ya=int(row['yes_ask_e4'])
        nb=int(row['no_bid_e4']); na=int(row['no_ask_e4'])
    except (KeyError,TypeError,ValueError,OverflowError):
        return 'UNAVAILABLE'
    if not (0<yb<ya<10000 and 0<nb<na<10000):
        return 'UNAVAILABLE'
    quantities=[]
    for key in ('yes_bid_quantity','yes_ask_quantity','no_bid_quantity','no_ask_quantity'):
        value=row.get(key)
        if not isinstance(value,int) or isinstance(value,bool) or value<0:
            return 'PRICES_ONLY'
        quantities.append(value)
    return 'BILATERAL_EXECUTABLE_READY' if all(value>0 for value in quantities) else 'PRICES_ONLY'

kind_counts=Counter()
pair_by_kind={'2':Counter(),'6':Counter()}
token_identity=Counter()
invalid_json=0
recent_rows=0
for path in selected:
    opener=gzip.open if str(path).endswith('.gz') else open
    try:
        with opener(path,'rt',encoding='utf-8') as stream:
            for line in stream:
                try:
                    row=json.loads(line)
                except ValueError:
                    invalid_json+=1
                    continue
                if row.get('schema')!='polymarket_v7_native_observation_v1':
                    continue
                kind=row.get('kind')
                if kind not in (2,6):
                    continue
                if wall_ns(row)<cutoff_ns:
                    continue
                recent_rows+=1
                kind_counts[str(kind)]+=1
                pair_by_kind[str(kind)][pair_state(row)]+=1
                if kind==2:
                    complete=bool(str(row.get('yes_token_id') or '')) and bool(str(row.get('no_token_id') or ''))
                    token_identity['COMPLETE' if complete else 'MISSING']+=1
    except (OSError,EOFError):
        truncated=True

print('V7_SSM_CAPTURE='+json.dumps({
  'window_seconds':900,
  'candidate_files':len(candidates),
  'scanned_files':len(selected),
  'scanned_bytes':selected_bytes,
  'scan_truncated':truncated,
  'invalid_json_rows':invalid_json,
  'recent_native_rows':recent_rows,
  'recent_kind_counts':dict(kind_counts),
  'pair_state_by_kind':{key:dict(value) for key,value in pair_by_kind.items()},
  'decision_token_identity':dict(token_identity),
  'runtime_capture_mode':status.get('native_capture_mode'),
  'native_observations_published':int(status.get('native_observations_published') or 0),
  'native_observations_written':int(status.get('native_observations_written') or 0),
  'native_observations_dropped':int(status.get('native_observations_dropped') or 0),
  'native_observations_queue_depth':int(status.get('native_observations_queue_depth') or 0),
},sort_keys=True,separators=(',',':')))
PY

echo "health_probe=metrics"
metrics="$(curl -fsS http://127.0.0.1:9108/metrics)"
for expected in   'polymarket_v7_execution_alive 1'   'polymarket_v7_single_writer_ok 1'   'polymarket_v7_exact_sha_ok 1'   'polymarket_v7_paper_only_contract_ok 1'   'polymarket_v7_authenticated_execution_disabled 1'   'polymarket_v7_live_algorithm_count 1'   'polymarket_v7_native_engine_mode 1'   'polymarket_v7_economic_new_risk_ready 0'; do
  if ! grep -Fxq "$expected" <<<"$metrics"; then
    echo "missing_metric_line=$expected" >&2
    exit 66
  fi
done
for metric in   polymarket_execution_opportunities   polymarket_execution_orders_submitted   polymarket_execution_fills   polymarket_execution_complete_fills   polymarket_execution_final_pnl_usd   polymarket_runtime_pnl_usd   polymarket_v7_canonical_submitted_units   polymarket_v7_canonical_complete_units; do
  if ! grep -Eq "^${metric}(\\{| )" <<<"$metrics"; then
    echo "missing_metric=$metric" >&2
    exit 67
  fi
done

echo "health_probe=prometheus"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
curl -fsS --get --data-urlencode 'query=up{job="polymarket-v7"}' \
  http://127.0.0.1:9090/api/v1/query | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("data",{}).get("result",[]); assert len(r)==1 and r[0]["value"][1]=="1", v'

echo "health_probe=grafana"
curl -fsS http://127.0.0.1:3000/api/health >/dev/null
python3 - "$APP" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])/'monitoring/grafana/dashboards'
for uid in ('polymarket-v7','polymarket-v7-multi-crypto'):
    path=root/f'{uid}.json'
    value=json.loads(path.read_text(encoding='utf-8'))
    assert value.get('uid')==uid, value
PY

echo "health_probe=full_health"
full_health="$(curl -sS http://127.0.0.1:9108/healthz 2>/dev/null || true)"
python3 - "$SHA" "$APP" "$ROOT" "$full_health" <<'PY'
import json,sys
sha,app,root,full=sys.argv[1:]
try:
    h=json.loads(full) if full else {}
except json.JSONDecodeError:
    h={}
print('V7_SSM_HEALTH='+json.dumps({
  'sha':sha,'app':app,'run_root':root,
  'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
  'core_runtime_healthy':True,
  'full_data_health_ok':bool(h.get('ok')),
  'full_data_health_reasons':h.get('reasons') if isinstance(h.get('reasons'),list) else [],
  'prometheus_ready':True,'grafana_local_ready':True,
},sort_keys=True,separators=(',',':')))
PY
"""
    return template.replace("__EXPECTED_SHA__", expected_sha)


def health(region: str, stack_name: str, expected_sha: str,
           expected_tailscale_ip: str, expected_instance_id: str) -> dict[str, Any]:
    if region != REGION or not exact_sha(expected_sha):
        raise SsmDeployError("eu-west-2 and exact SHA required")
    identity = aws_json(region, ["sts", "get-caller-identity"])
    candidates = candidate_instances(region, stack_name)
    probes = probe(region, candidates)
    selected = select_target(probes, expected_tailscale_ip, expected_instance_id)
    stdout, stderr = run(region, selected["instance_id"], health_command(expected_sha), 180)
    runtime = parse_marker(stdout, "V7_SSM_HEALTH=")
    capture = parse_marker(stdout, "V7_SSM_CAPTURE=")
    runtime["bilateral_capture"] = capture
    if runtime.get("sha") != expected_sha or runtime.get("core_runtime_healthy") is not True:
        raise SsmDeployError("London health receipt mismatch")
    return {
        "schema": SCHEMA,
        "expected_sha": expected_sha,
        "region": region,
        "aws_account": identity.get("Account"),
        "selected": selected,
        "runtime": runtime,
        "stderr_tail": stderr[-2000:],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--expected-tailscale-ip",default="")
    p.add_argument("--expected-instance-id",default="")
    p.add_argument("--region",default=REGION)
    p.add_argument("--stack-name",default=STACK)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    try:
        receipt=health(a.region,a.stack_name,a.expected_sha,a.expected_tailscale_ip,a.expected_instance_id)
    except (OSError,ValueError,SsmDeployError) as exc:
        p.exit(2,f"v7_london_ssm_health: {exc}\n")
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print("ssm_health_result=success")
    print("healthy_sha="+receipt["expected_sha"])
    print("full_data_health_ok="+str(receipt["runtime"]["full_data_health_ok"]).lower())
    return 0


if __name__=="__main__":
    raise SystemExit(main())
