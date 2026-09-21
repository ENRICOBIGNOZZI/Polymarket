#!/usr/bin/env python3
"""Read-only health probe for the independent London collection plane."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from v7_collection_plane_ssm import REGION, SsmDeployError, run

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")

REMOTE = r"""set -euo pipefail
ROOT=/mnt/polymarket-data/polymarket_v7_collection
UNIT=polymarket-v7-collection.service
systemctl is-active --quiet "$UNIT"
python3 - "$ROOT" <<'PY'
import json,re,sys,time
from pathlib import Path
root=Path(sys.argv[1])
runtime=json.loads((root/'control/runtime_status.json').read_text())
universe=json.loads((root/'universe/status.json').read_text())
external=json.loads((root/'external_fair/all_assets_status.json').read_text())
book=json.loads((root/'research/repricing_book/fillability_ws_status.json').read_text())
sha=str(runtime.get('collector_sha') or '')
assert re.fullmatch(r'[0-9a-f]{40}',sha)
assert runtime.get('model_independent') is True
assert runtime.get('live_model_required') is False
assert runtime.get('execution_authority')=='ZERO_AUTHORITY_DATA_COLLECTION'
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
assert runtime.get('real_capital_at_risk') is False
assert runtime.get('state')=='COLLECTING'
assert time.time_ns()-int(runtime.get('timestamp_ns') or 0)<10_000_000_000
assert universe.get('model_sha')==sha and universe.get('state')=='OPERATIONAL'
assert int(universe.get('book_selection_contexts') or 0)==30
assert int(universe.get('book_selection_tokens') or 0)==60
assert external.get('model_sha')==sha and external.get('state')=='OPERATIONAL'
assert int(external.get('ready_assets') or 0)==6
assert book.get('model_sha')==sha and book.get('state')=='running'
assert int(book.get('observed_tokens') or 0)>0
assert int(book.get('dropped_events') or 0)==0
assert int(book.get('decoder_failures') or 0)==0

def bytes_now():
    paths=[]
    for pattern in (
        'external_fair/raw/*',
        'external_fair/assets/*/raw/*',
        'external_fair/normalized_events/*',
        'external_fair/assets/*/normalized_events/*',
        'research/repricing_book/book_observations/*',
    ):
        paths.extend(root.glob(pattern))
    return sum(p.stat().st_size for p in paths if p.is_file() and not p.is_symlink())

before=bytes_now()
time.sleep(10)
after=bytes_now()
assert after>before
result={
  'schema':'polymarket_v7_collection_plane_health_v1',
  'collector_sha':sha,
  'collection_root':str(root),
  'model_independent':True,
  'paper_only':True,
  'authenticated_execution':False,
  'real_order_submission':False,
  'real_capital_at_risk':False,
  'execution_authority':'ZERO_AUTHORITY_DATA_COLLECTION',
  'external_ready_assets':int(external.get('ready_assets') or 0),
  'book_observed_tokens':int(book.get('observed_tokens') or 0),
  'growth_bytes_10s':after-before,
  'timestamp_ns':time.time_ns(),
}
print('V7_COLLECTION_HEALTH='+json.dumps(result,sort_keys=True,separators=(',',':')))
PY
"""


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--region",default=REGION)
    args=parser.parse_args(argv)
    if args.region!=REGION:
        parser.error("collection health must remain eu-west-2")
    if not INSTANCE_RE.fullmatch(args.instance_id):
        parser.error("valid instance id required")
    try:
        stdout,stderr=run(args.region,args.instance_id,REMOTE,120)
        rows=[line for line in stdout.splitlines() if line.startswith("V7_COLLECTION_HEALTH=")]
        if len(rows)!=1:
            raise SsmDeployError("collection health marker missing")
        value=json.loads(rows[0].split("=",1)[1])
        if (
            value.get("schema")!="polymarket_v7_collection_plane_health_v1"
            or value.get("model_independent") is not True
            or value.get("execution_authority")!="ZERO_AUTHORITY_DATA_COLLECTION"
            or int(value.get("growth_bytes_10s") or 0)<=0
        ):
            raise SsmDeployError("collection health receipt invalid")
    except (OSError,ValueError,json.JSONDecodeError,SsmDeployError) as exc:
        parser.exit(2,f"collection health failed: {exc}\n")
    value["stderr_tail"]=stderr[-1000:]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print("collection_health=PASS")
    print("growth_bytes_10s="+str(value["growth_bytes_10s"]))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
