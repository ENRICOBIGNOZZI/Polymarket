#!/usr/bin/env python3
"""Read-only health audit for the London executable-markout forward shadow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import statistics

from v7_london_ssm_deploy import REGION, run

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")


def load_request(path: Path) -> dict:
    value=json.loads(path.read_text(encoding="utf-8"))
    required={
        "schema","version","request_id","instance_id","expected_parent_sha",
        "paper_only","authenticated_execution","real_order_submission","real_capital_at_risk",
    }
    if set(value)!=required:
        raise ValueError("unexpected request fields")
    if value["schema"]!="polymarket_v7_executable_markout_shadow_health_request_v1" or value["version"]!=1:
        raise ValueError("invalid request schema")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}",value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not re.fullmatch(r"[0-9a-f]{40}",value["expected_parent_sha"]):
        raise ValueError("invalid expected parent sha")
    if not (
        value["paper_only"] is True and value["authenticated_execution"] is False
        and value["real_order_submission"] is False and value["real_capital_at_risk"] is False
    ):
        raise ValueError("safety contract violated")
    return value


REMOTE=r"""
import json,shlex,statistics,subprocess
from collections import Counter,deque
from pathlib import Path

unit='polymarket-v7-paper.service'
shadow='polymarket-v7-executable-markout-shadow.service'
env=subprocess.check_output(['systemctl','show',unit,'-p','Environment','--value'],text=True)
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT='))).resolve()
runtime=json.loads((root/'control/runtime_status.json').read_text())
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
paper_pid=int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip())
shadow_active=subprocess.check_output(['systemctl','is-active',shadow],text=True).strip()
shadow_pid=int(subprocess.check_output(['systemctl','show',shadow,'-p','MainPID','--value'],text=True).strip())
folder=root/'research/executable_markout_forward_shadow'
status_path=folder/'status.json'
pred_path=folder/'predictions.jsonl'
status=json.loads(status_path.read_text()) if status_path.is_file() else {}

tail=deque(maxlen=5000)
if pred_path.is_file():
    with pred_path.open(encoding='utf-8') as stream:
        for line in stream:
            try: tail.append(json.loads(line))
            except ValueError: pass

ages=[row.get('inference_age_ns')/1e6 for row in tail if isinstance(row.get('inference_age_ns'),int)]
def q(values,p):
    if not values:return None
    ordered=sorted(values)
    return ordered[round((len(ordered)-1)*p)]
assets=Counter(str(row.get('asset') or 'UNKNOWN') for row in tail)
eligible=sum(row.get('forward_eligible') is True for row in tail)
positive={h:sum(float((row.get('predictions') or {}).get(str(h),-1e99))>0 for row in tail) for h in (500,1000,2000)}
result={
 'schema':'polymarket_v7_executable_markout_shadow_health_v1',
 'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'real_capital_at_risk':False,
 'paper_pid':paper_pid,'shadow_active':shadow_active,'shadow_pid':shadow_pid,
 'status':status,
 'prediction_file_bytes':pred_path.stat().st_size if pred_path.is_file() else 0,
 'sample_rows':len(tail),
 'age_ms':{'p50':q(ages,.5),'p90':q(ages,.9),'p95':q(ages,.95),'p99':q(ages,.99),'max':max(ages) if ages else None},
 'forward_eligible_sample':eligible,
 'forward_eligible_fraction':eligible/len(tail) if tail else None,
 'asset_counts':dict(sorted(assets.items())),
 'positive_prediction_counts':positive,
}
print('SHADOW_HEALTH='+json.dumps(result,sort_keys=True,separators=(',',':')))
"""


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--request",type=Path,required=True)
    args=ap.parse_args()
    req=load_request(args.request)
    stdout,_=run(REGION,req["instance_id"],"python3 -c "+shlex.quote(REMOTE),60)
    line=next(x for x in stdout.splitlines() if x.startswith("SHADOW_HEALTH="))
    value=json.loads(line.split("=",1)[1])
    if not (
        value.get("paper_only") is True and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False and value.get("real_capital_at_risk") is False
    ):
        raise RuntimeError("unsafe health response")
    print(json.dumps(value,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
