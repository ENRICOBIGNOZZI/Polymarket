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
if ! systemctl is-active --quiet "$UNIT"; then
  echo "COLLECTION_SERVICE_INACTIVE"
  systemctl --no-pager --full status "$UNIT" || true
  echo "--- collection journal ---"
  journalctl -u "$UNIT" -n 120 --no-pager -o cat || true
  for log in \
    "$ROOT/public_https_proxy.log" \
    "$ROOT/universe/collector.log" \
    "$ROOT/external_fair/external_assets_supervisor.log" \
    "$ROOT/external_fair/rtds_monitor.log" \
    "$ROOT/research/repricing_book_observer.log" \
    "$ROOT/trade_recorder.log"; do
    echo "--- $log ---"
    tail -n 60 "$log" 2>/dev/null || true
  done
  exit 41
fi
python3 - "$ROOT" <<'PY'
import json,re,sys,time
from pathlib import Path
root=Path(sys.argv[1])
runtime=json.loads((root/'control/runtime_status.json').read_text())
universe=json.loads((root/'universe/status.json').read_text())
external=json.loads((root/'external_fair/all_assets_status.json').read_text())
book=json.loads((root/'research/repricing_book/fillability_ws_status.json').read_text())
sha=str(runtime.get('collector_sha') or '')

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

checks={}
checks['sha_valid']=bool(re.fullmatch(r'[0-9a-f]{40}',sha))
checks['model_independent']=runtime.get('model_independent') is True
checks['live_model_not_required']=runtime.get('live_model_required') is False
checks['zero_authority']=runtime.get('execution_authority')=='ZERO_AUTHORITY_DATA_COLLECTION'
checks['paper_only']=runtime.get('paper_only') is True
checks['authenticated_execution_disabled']=runtime.get('authenticated_execution') is False
checks['real_order_submission_disabled']=runtime.get('real_order_submission') is False
checks['real_capital_at_risk_false']=runtime.get('real_capital_at_risk') is False
checks['runtime_collecting']=runtime.get('state')=='COLLECTING'
checks['runtime_fresh']=time.time_ns()-int(runtime.get('timestamp_ns') or 0)<10_000_000_000
checks['universe_operational']=universe.get('model_sha')==sha and universe.get('state')=='OPERATIONAL'
checks['context_count_30']=int(universe.get('book_selection_contexts') or 0)==30
checks['token_count_60']=int(universe.get('book_selection_tokens') or 0)==60
checks['external_sha_match']=external.get('model_sha')==sha
checks['external_operational']=external.get('state')=='OPERATIONAL'
checks['external_all_assets_ready']=int(external.get('ready_assets') or 0)==6
checks['book_running']=book.get('model_sha')==sha and book.get('state')=='running'
checks['book_has_tokens']=int(book.get('observed_tokens') or 0)>0
checks['book_no_drops']=int(book.get('dropped_events') or 0)==0
checks['book_no_decoder_failures']=int(book.get('decoder_failures') or 0)==0

before=bytes_now()
time.sleep(10)
after=bytes_now()
growth=after-before
checks['tape_growth_positive']=growth>0

core_names=(
    'sha_valid','model_independent','live_model_not_required','zero_authority',
    'paper_only','authenticated_execution_disabled','real_order_submission_disabled',
    'real_capital_at_risk_false','runtime_collecting','runtime_fresh',
    'universe_operational','context_count_30','token_count_60',
    'book_running','book_has_tokens','book_no_drops','book_no_decoder_failures',
    'tape_growth_positive',
)
full_names=core_names+(
    'external_sha_match','external_operational','external_all_assets_ready',
)
core_ok=all(checks[name] for name in core_names)
full_ok=all(checks[name] for name in full_names)

assets=[]
for row in external.get('assets') or []:
    if isinstance(row,dict):
        assets.append({
            'asset':row.get('asset'),
            'alive':row.get('alive'),
            'data_ready':row.get('data_ready'),
            'reason':row.get('reason'),
            'capture_recovery_pending':row.get('capture_recovery_pending'),
            'restart_count':row.get('restart_count'),
        })

result={
  'schema':'polymarket_v7_collection_plane_health_v2',
  'collector_sha':sha,
  'collection_root':str(root),
  'model_independent':True,
  'paper_only':True,
  'authenticated_execution':False,
  'real_order_submission':False,
  'real_capital_at_risk':False,
  'execution_authority':'ZERO_AUTHORITY_DATA_COLLECTION',
  'core_collection_health_ok':core_ok,
  'full_data_health_ok':full_ok,
  'checks':checks,
  'external_state':external.get('state'),
  'external_ready_assets':int(external.get('ready_assets') or 0),
  'external_missing_assets':external.get('missing_assets') or [],
  'external_assets':assets,
  'book_state':book.get('state'),
  'book_observed_tokens':int(book.get('observed_tokens') or 0),
  'growth_bytes_10s':growth,
  'timestamp_ns':time.time_ns(),
}
print('V7_COLLECTION_HEALTH='+json.dumps(result,sort_keys=True,separators=(',',':')))
if not core_ok:
    raise SystemExit(42)
if not full_ok:
    raise SystemExit(43)
PY"""


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
            value.get("schema")!="polymarket_v7_collection_plane_health_v2"
            or value.get("model_independent") is not True
            or value.get("execution_authority")!="ZERO_AUTHORITY_DATA_COLLECTION"
            or value.get("core_collection_health_ok") is not True
            or value.get("full_data_health_ok") is not True
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
