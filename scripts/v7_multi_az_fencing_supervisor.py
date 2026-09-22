#!/usr/bin/env python3
"""Long-running fail-closed multi-AZ lease supervisor for the V7 PAPER runtime."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path

SCHEMA="polymarket_v7_multi_az_fencing_supervisor_v1"
_STOP=False

def stop(*_):
    global _STOP;_STOP=True

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(path)

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root",type=Path,required=True)
    ap.add_argument("--run-root",type=Path,required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--server-id",required=True)
    ap.add_argument("--owner-id",required=True);ap.add_argument("--lease-key",default="polymarket-v7-paper")
    ap.add_argument("--ttl-seconds",type=int,default=15);ap.add_argument("--interval-seconds",type=float,default=5)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or not args.server_id or not args.owner_id:
        raise SystemExit("invalid fencing identity")
    receipt=args.run_root/"control/fencing_receipt.json"
    status=args.run_root/"control/fencing_supervisor_status.json"
    table=os.environ.get("PM_V7_FENCING_TABLE","").strip()
    required=os.environ.get("PM_V7_MULTI_AZ_FENCING_REQUIRED","").lower() in {"1","true","yes"}
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    if required and not table:
        atomic(status,{"schema":SCHEMA,"state":"REQUIRED_TABLE_MISSING","safe":False,
                       "paper_only":True,"model_sha":args.model_sha})
        return 2
    if not table:
        while not _STOP:
            atomic(status,{"schema":SCHEMA,"state":"NOT_REQUIRED_SINGLE_NODE","safe":True,
                           "paper_only":True,"authenticated_execution":False,
                           "real_order_submission":False,"model_sha":args.model_sha,
                           "server_id":args.server_id,"timestamp_ms":time.time_ns()//1_000_000})
            time.sleep(args.interval_seconds)
        return 0
    lease=args.repository_root/"ops/v7_multi_az_fencing_lease.py"
    guard=args.repository_root/"scripts/v7_multi_az_fencing_guard.py"
    action="acquire"
    try:
        while not _STOP:
            cmd=[sys.executable,str(lease),action,"--table",table,"--lease-key",args.lease_key,
                 "--owner-id",args.owner_id,"--server-id",args.server_id,
                 "--model-sha",args.model_sha,"--ttl-seconds",str(args.ttl_seconds),
                 "--output",str(receipt)]
            cp=subprocess.run(cmd,cwd=args.repository_root,text=True,capture_output=True)
            if cp.returncode!=0:
                atomic(status,{"schema":SCHEMA,"state":"LEASE_ACQUIRE_OR_RENEW_FAILED",
                               "safe":False,"paper_only":True,"model_sha":args.model_sha,
                               "server_id":args.server_id,"timestamp_ms":time.time_ns()//1_000_000})
                return 2
            gp=subprocess.run([
                sys.executable,str(guard),"--receipt",str(receipt),
                "--model-sha",args.model_sha,"--server-id",args.server_id,
                "--output",str(status),"--minimum-remaining-ms","5000","--required"],
                cwd=args.repository_root,text=True,capture_output=True)
            if gp.returncode!=0:return 2
            action="renew";time.sleep(args.interval_seconds)
    finally:
        if table and receipt.exists():
            subprocess.run([
                sys.executable,str(lease),"release","--table",table,
                "--lease-key",args.lease_key,"--owner-id",args.owner_id,
                "--server-id",args.server_id,"--model-sha",args.model_sha,
                "--ttl-seconds",str(args.ttl_seconds),"--output",str(receipt)],
                cwd=args.repository_root,text=True,capture_output=True)
    return 0

if __name__=="__main__":raise SystemExit(main())
