#!/usr/bin/env python3
"""Read-only London evaluator for executable-markout forward shadow."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
from pathlib import Path
import re
import shlex

from v7_london_ssm_deploy import REGION, run

INSTANCE_RE=re.compile(r"^i-[0-9a-f]+$")


def load_request(path:Path)->dict:
    value=json.loads(path.read_text(encoding="utf-8"))
    required={
        "schema","version","request_id","instance_id","expected_parent_sha",
        "paper_only","authenticated_execution","real_order_submission","real_capital_at_risk",
    }
    if set(value)!=required: raise ValueError("unexpected request fields")
    if value["schema"]!="polymarket_v7_executable_markout_forward_eval_request_v1" or value["version"]!=1:
        raise ValueError("invalid schema")
    if not INSTANCE_RE.fullmatch(value["instance_id"]): raise ValueError("invalid instance")
    if not re.fullmatch(r"[0-9a-f]{40}",value["expected_parent_sha"]): raise ValueError("invalid parent")
    if not (
        value["paper_only"] is True and value["authenticated_execution"] is False
        and value["real_order_submission"] is False and value["real_capital_at_risk"] is False
    ): raise ValueError("unsafe request")
    return value


REMOTE=r"""
import base64,gzip,json,shlex,subprocess,sys,tempfile
from pathlib import Path
env=subprocess.check_output(['systemctl','show','polymarket-v7-paper.service','-p','Environment','--value'],text=True)
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT='))).resolve()
runtime=json.loads((root/'control/runtime_status.json').read_text())
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
pred=root/'research/executable_markout_forward_shadow/predictions.jsonl'
assert pred.is_file()
native=sorted((root/'research/native_observations').glob('*/*.jsonl'))
assert native

with tempfile.TemporaryDirectory(prefix='pm-forward-eval-') as tmp:
    tmp=Path(tmp)
    shadow=tmp/'shadow.py';shadow.write_bytes(gzip.decompress(base64.b64decode(SHADOW)))
    evaluator=tmp/'evaluate.py';evaluator.write_bytes(gzip.decompress(base64.b64decode(EVALUATOR)))
    sys.path.insert(0,str(REPO))
    sys.path.insert(0,str(tmp))
    import importlib.util
    spec=importlib.util.spec_from_file_location('scripts.v7_executable_markout_forward_shadow',shadow)
    sm=importlib.util.module_from_spec(spec);sys.modules['scripts.v7_executable_markout_forward_shadow']=sm;spec.loader.exec_module(sm)
    spec2=importlib.util.spec_from_file_location('forward_eval',evaluator)
    ev=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(ev)
    predictions=ev.load_predictions(pred)
    origins,labels=ev.load_native(native,predictions)
    result=ev.evaluate(predictions,origins,labels)

summary={
 'schema':'polymarket_v7_executable_markout_forward_eval_summary_v1',
 'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'real_capital_at_risk':False,
 'prediction_count':result['prediction_count'],
 'origin_join_count':result['origin_join_count'],
 'native_label_count':result['native_label_count'],
 'live_geometry':result['live_geometry'],
 'cells':result['cells'],
}
print('FORWARD_EVAL='+json.dumps(summary,sort_keys=True,separators=(',',':')))
"""


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--request",type=Path,required=True)
    args=ap.parse_args()
    req=load_request(args.request)
    repo=Path(__file__).resolve().parents[1]
    shadow=base64.b64encode(gzip.compress((repo/"scripts/v7_executable_markout_forward_shadow.py").read_bytes())).decode()
    evaluator=base64.b64encode(gzip.compress((repo/"scripts/v7_executable_markout_forward_evaluate.py").read_bytes())).decode()
    source=(
        "SHADOW="+repr(shadow)+"\n"
        +"EVALUATOR="+repr(evaluator)+"\n"
        +"REPO="+repr(str(repo))+"\n"
        +REMOTE
    )
    stdout,_=run(REGION,req["instance_id"],"python3 -c "+shlex.quote(source),300)
    line=next(x for x in stdout.splitlines() if x.startswith("FORWARD_EVAL="))
    value=json.loads(line.split("=",1)[1])
    if not (
        value.get("paper_only") is True and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False and value.get("real_capital_at_risk") is False
    ): raise RuntimeError("unsafe response")
    print(json.dumps(value,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
