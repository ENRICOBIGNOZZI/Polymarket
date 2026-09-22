#!/usr/bin/env python3
"""Acquire/renew/release a DynamoDB-backed V7 single-writer lease.

Control-plane only. Requires AWS CLI credentials already authorized by the
deployment environment. Every successful write increments a fencing token.
"""
from __future__ import annotations
import argparse,json,os,subprocess,time
from pathlib import Path

SCHEMA="polymarket_v7_multi_az_fencing_receipt_v1"

def run_aws(args:list[str])->dict:
    cp=subprocess.run(["aws",*args],check=True,text=True,capture_output=True)
    return json.loads(cp.stdout or "{}")

def n(v:int)->dict:return {"N":str(v)}
def s(v:str)->dict:return {"S":v}

def value(attr:dict|None):
    if not isinstance(attr,dict):return None
    if "S" in attr:return attr["S"]
    if "N" in attr:
        try:return int(attr["N"])
        except ValueError:return None
    return None

def write(path:Path,payload:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)

def update(table:str,key:str,owner:str,server_id:str,sha:str,ttl_ms:int,release:bool)->dict:
    now=int(time.time()*1000)
    until=now if release else now+ttl_ms
    expr=("SET owner_id=:owner, server_id=:server, model_sha=:sha, "
          "lease_until_epoch_ms=:until, updated_epoch_ms=:now ADD fencing_token :one")
    condition=("attribute_not_exists(lease_until_epoch_ms) OR "
               "lease_until_epoch_ms < :now OR owner_id = :owner")
    values={
        ":owner":s(owner),":server":s(server_id),":sha":s(sha),
        ":until":n(until),":now":n(now),":one":n(1),
    }
    out=run_aws([
        "dynamodb","update-item","--table-name",table,
        "--key",json.dumps({"lease_key":s(key)},separators=(",",":")),
        "--update-expression",expr,
        "--condition-expression",condition,
        "--expression-attribute-values",json.dumps(values,separators=(",",":")),
        "--return-values","ALL_NEW","--output","json",
    ])
    attrs=out.get("Attributes") or {}
    return {
        "schema":SCHEMA,"version":1,"paper_only":True,
        "authenticated_execution":False,"real_order_submission":False,
        "provider":"AWS_DYNAMODB","lease_key":key,
        "owner_id":value(attrs.get("owner_id")),
        "server_id":value(attrs.get("server_id")),
        "model_sha":value(attrs.get("model_sha")),
        "lease_until_epoch_ms":value(attrs.get("lease_until_epoch_ms")),
        "updated_epoch_ms":value(attrs.get("updated_epoch_ms")),
        "fencing_token":value(attrs.get("fencing_token")),
        "released":release,
    }

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action",choices=["acquire","renew","release"])
    ap.add_argument("--table",default=os.environ.get("PM_V7_FENCING_TABLE",""))
    ap.add_argument("--lease-key",default="polymarket-v7-paper")
    ap.add_argument("--owner-id",required=True);ap.add_argument("--server-id",required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--ttl-seconds",type=int,default=15)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    if not args.table or len(args.model_sha)!=40 or not(5<=args.ttl_seconds<=120):
        raise SystemExit("invalid fencing arguments")
    receipt=update(
        args.table,args.lease_key,args.owner_id,args.server_id,args.model_sha,
        args.ttl_seconds*1000,args.action=="release")
    write(args.output,receipt)
    return 0

if __name__=="__main__":raise SystemExit(main())
