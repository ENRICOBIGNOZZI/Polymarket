#!/usr/bin/env python3
"""Validate a multi-AZ fencing receipt before granting runtime readiness."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path

SCHEMA="polymarket_v7_multi_az_fencing_guard_v1"
RECEIPT="polymarket_v7_multi_az_fencing_receipt_v1"

def load(path:Path):
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def validate(receipt:dict,sha:str,server_id:str,minimum_ms:int,required:bool)->dict:
    now=time.time_ns()//1_000_000
    if not required:
        return {"schema":SCHEMA,"state":"NOT_REQUIRED_SINGLE_NODE","safe":True,
                "paper_only":True,"model_sha":sha,"timestamp_ms":now}
    safe=(receipt.get("schema")==RECEIPT and receipt.get("paper_only") is True
          and receipt.get("authenticated_execution") is False
          and receipt.get("real_order_submission") is False
          and receipt.get("released") is not True
          and receipt.get("model_sha")==sha and receipt.get("server_id")==server_id)
    try:
        token=int(receipt.get("fencing_token") or 0)
        until=int(receipt.get("lease_until_epoch_ms") or 0)
    except (TypeError,ValueError):token,until=0,0
    safe=safe and token>0 and until-now>=minimum_ms
    return {"schema":SCHEMA,"state":"SAFE" if safe else "UNSAFE_OR_STALE_LEASE",
            "safe":bool(safe),"paper_only":True,"model_sha":sha,"server_id":server_id,
            "timestamp_ms":now,"fencing_token":token,"lease_until_epoch_ms":until,
            "lease_remaining_ms":max(0,until-now)}

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt",type=Path,required=True);ap.add_argument("--model-sha",required=True)
    ap.add_argument("--server-id",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--minimum-remaining-ms",type=int,default=5000)
    ap.add_argument("--required",action="store_true")
    args=ap.parse_args()
    value=validate(load(args.receipt),args.model_sha,args.server_id,args.minimum_remaining_ms,args.required)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix(args.output.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
    return 0 if value["safe"] else 2

if __name__=="__main__":raise SystemExit(main())
