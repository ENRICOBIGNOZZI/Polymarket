#!/usr/bin/env python3
"""DynamoDB-backed active/passive lease with monotonic fencing token.

Cold control-plane utility. It never starts execution. The runtime may consume
a fresh receipt only after the deployment creates the configured DynamoDB table.
"""
from __future__ import annotations
import argparse,json,subprocess,time
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_aws_fencing_lease_v1"

def aws(args:list[str],region:str)->dict[str,Any]:
    proc=subprocess.run(["aws","--region",region,*args],check=False,capture_output=True,text=True,timeout=10)
    if proc.returncode:
        raise RuntimeError("aws_cli_failure")
    try:v=json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:raise RuntimeError("aws_json_failure")
    return v if isinstance(v,dict) else {}

def sval(x:str)->str:return json.dumps({"S":x},separators=(",",":"))
def nval(x:int)->str:return json.dumps({"N":str(x)},separators=(",",":"))

def acquire(table:str,lease_id:str,owner:str,sha:str,run_id:str,region:str,lease_ms:int)->dict[str,Any]:
    now=time.time_ns()//1_000_000;until=now+lease_ms
    values=json.dumps({
        ":owner":{"S":owner},":sha":{"S":sha},":run":{"S":run_id},
        ":now":{"N":str(now)},":until":{"N":str(until)},":one":{"N":"1"},
    },separators=(",",":"))
    out=aws(["dynamodb","update-item","--table-name",table,
             "--key",json.dumps({"lease_id":{"S":lease_id}},separators=(",",":")),
             "--update-expression","SET owner_id=:owner, model_sha=:sha, run_id=:run, lease_until_ms=:until ADD fencing_token :one",
             "--condition-expression","attribute_not_exists(lease_until_ms) OR lease_until_ms < :now OR owner_id = :owner",
             "--expression-attribute-values",values,"--return-values","ALL_NEW"],region)
    attrs=out.get("Attributes") or {}
    token=int(((attrs.get("fencing_token") or {}).get("N") or "0"))
    observed_until=int(((attrs.get("lease_until_ms") or {}).get("N") or "0"))
    if token<=0 or observed_until!=until:raise RuntimeError("invalid_lease_receipt")
    return {"fencing_token":token,"lease_until_ms":until,"acquired_at_ms":now}

def renew(table:str,lease_id:str,owner:str,token:int,region:str,lease_ms:int)->dict[str,Any]:
    now=time.time_ns()//1_000_000;until=now+lease_ms
    values=json.dumps({":owner":{"S":owner},":token":{"N":str(token)},":until":{"N":str(until)}},separators=(",",":"))
    out=aws(["dynamodb","update-item","--table-name",table,
             "--key",json.dumps({"lease_id":{"S":lease_id}},separators=(",",":")),
             "--update-expression","SET lease_until_ms=:until",
             "--condition-expression","owner_id=:owner AND fencing_token=:token",
             "--expression-attribute-values",values,"--return-values","ALL_NEW"],region)
    attrs=out.get("Attributes") or {}
    observed=int(((attrs.get("lease_until_ms") or {}).get("N") or "0"))
    if observed!=until:raise RuntimeError("invalid_renew_receipt")
    return {"fencing_token":token,"lease_until_ms":until,"renewed_at_ms":now}

def atomic(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(path)

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table",required=True);ap.add_argument("--lease-id",required=True);ap.add_argument("--owner",required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--run-id",required=True);ap.add_argument("--region",default="eu-west-2")
    ap.add_argument("--lease-ms",type=int,default=15000);ap.add_argument("--renew-ms",type=int,default=5000)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    try:r=acquire(a.table,a.lease_id,a.owner,a.model_sha,a.run_id,a.region,a.lease_ms)
    except Exception as exc:
        atomic(a.output,{"schema":SCHEMA,"state":"FENCING_UNAVAILABLE","paper_only":True,
                         "model_sha":a.model_sha,"owner":a.owner,"error":type(exc).__name__})
        return 2
    token=r["fencing_token"]
    while True:
        atomic(a.output,{"schema":SCHEMA,"state":"LEASE_HELD","paper_only":True,
                         "authenticated_execution":False,"real_order_submission":False,
                         "model_sha":a.model_sha,"run_id":a.run_id,"owner":a.owner,
                         "region":a.region,**r})
        time.sleep(a.renew_ms/1000.0)
        try:r=renew(a.table,a.lease_id,a.owner,token,a.region,a.lease_ms)
        except Exception as exc:
            atomic(a.output,{"schema":SCHEMA,"state":"LEASE_LOST","paper_only":True,
                             "authenticated_execution":False,"real_order_submission":False,
                             "model_sha":a.model_sha,"run_id":a.run_id,"owner":a.owner,
                             "fencing_token":token,"error":type(exc).__name__})
            return 3

if __name__=="__main__":raise SystemExit(main())
