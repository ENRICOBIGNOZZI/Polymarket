#!/usr/bin/env python3
"""Persistent fail-closed guard for the canonical AWS multi-AZ fencing lease."""
from __future__ import annotations
import argparse,json,os,time
from pathlib import Path

SCHEMA="polymarket_v7_multi_az_fencing_guard_v1"
RECEIPT="polymarket_v7_aws_fencing_lease_v1"

def load(path:Path):
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def validate(receipt:dict,sha:str,owner:str,minimum_ms:int,required:bool)->dict:
    now=time.time_ns()//1_000_000
    if not required:
        return {"schema":SCHEMA,"state":"NOT_REQUIRED_SINGLE_NODE","safe":True,
                "paper_only":True,"authenticated_execution":False,
                "real_order_submission":False,"model_sha":sha,"timestamp_ms":now}
    safe=(receipt.get("schema")==RECEIPT
          and receipt.get("state")=="LEASE_HELD"
          and receipt.get("paper_only") is True
          and receipt.get("authenticated_execution") is False
          and receipt.get("real_order_submission") is False
          and receipt.get("model_sha")==sha and receipt.get("owner")==owner)
    try:
        token=int(receipt.get("fencing_token") or 0)
        until=int(receipt.get("lease_until_ms") or 0)
    except (TypeError,ValueError):token,until=0,0
    safe=safe and token>0 and until-now>=minimum_ms
    return {"schema":SCHEMA,"state":"SAFE" if safe else "UNSAFE_OR_STALE_LEASE",
            "safe":bool(safe),"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"model_sha":sha,"owner":owner,
            "timestamp_ms":now,"fencing_token":token,"lease_until_ms":until,
            "lease_remaining_ms":max(0,until-now)}

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt",type=Path,required=True);ap.add_argument("--model-sha",required=True)
    ap.add_argument("--owner-id",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--kill-marker",type=Path,required=True)
    ap.add_argument("--minimum-remaining-ms",type=int,default=5000)
    ap.add_argument("--interval-ms",type=int,default=1000)
    ap.add_argument("--required",action="store_true")
    args=ap.parse_args()
    if not (100<=args.interval_ms<=5000 and 1000<=args.minimum_remaining_ms<=60000):
        raise SystemExit("invalid fencing guard bounds")
    while True:
        value=validate(
            load(args.receipt),args.model_sha,args.owner_id,
            args.minimum_remaining_ms,args.required)
        atomic(args.output,value)
        if args.required and value["safe"] is not True:
            atomic(args.kill_marker,{
                "schema":"polymarket_v7_runtime_failure_v1",
                "paper_only":True,"authenticated_execution":False,
                "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000,
                "reason":"MULTI_AZ_FENCING_LEASE_LOST",
                "fencing_state":value["state"],
            })
            return 2
        time.sleep(args.interval_ms/1000.0)

if __name__=="__main__":raise SystemExit(main())
