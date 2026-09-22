#!/usr/bin/env python3
"""Long-running wrapper for the canonical AWS active/passive fencing lease.

When multi-AZ fencing is disabled this remains an idle, healthy control process.
When enabled it launches exactly one canonical DynamoDB lease worker and mirrors
its state. Lease loss is fatal and propagated through process exit.
"""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path

SCHEMA="polymarket_v7_multi_az_fencing_supervisor_v1"
_STOP=False

def stop(*_):
    global _STOP;_STOP=True

def load(path:Path)->dict:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root",type=Path,required=True)
    ap.add_argument("--run-root",type=Path,required=True)
    ap.add_argument("--config",type=Path,required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--server-id",required=True)
    ap.add_argument("--run-id",required=True)
    ap.add_argument("--interval-seconds",type=float,default=1.0)
    args=ap.parse_args()
    cfg=load(args.config)
    if (cfg.get("schema")!="polymarket_v7_failover_fencing_v1"
        or cfg.get("paper_only") is not True or len(args.model_sha)!=40):
        raise SystemExit("invalid fencing config")
    enabled=cfg.get("enabled") is True
    table_env=str(cfg.get("table_name_environment") or "PM_V7_FENCING_TABLE")
    table=os.environ.get(table_env,"").strip()
    receipt=args.run_root/"control/az_fencing_lease.json"
    status=args.run_root/"control/fencing_supervisor_status.json"
    owner=f"{args.run_id}:{args.server_id}"
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    if not enabled:
        while not _STOP:
            atomic(status,{"schema":SCHEMA,"state":"DISABLED_SINGLE_ACTIVE_NODE","safe":True,
                           "paper_only":True,"authenticated_execution":False,
                           "real_order_submission":False,"model_sha":args.model_sha,
                           "owner":owner,"timestamp_ms":time.time_ns()//1_000_000})
            time.sleep(args.interval_seconds)
        return 0
    if not table:
        atomic(status,{"schema":SCHEMA,"state":"FENCING_TABLE_MISSING","safe":False,
                       "paper_only":True,"model_sha":args.model_sha,"owner":owner})
        return 2
    worker=args.repository_root/"ops/v7_aws_fencing_lease.py"
    lease_id=str(cfg.get("lease_id") or "")
    lease_ms=int(cfg.get("lease_duration_ms") or 15000)
    renew_ms=int(cfg.get("renew_interval_ms") or 5000)
    region=str(cfg.get("region") or "eu-west-2")
    proc=subprocess.Popen([
        sys.executable,str(worker),"--table",table,"--lease-id",lease_id,
        "--owner",owner,"--model-sha",args.model_sha,"--run-id",args.run_id,
        "--region",region,"--lease-ms",str(lease_ms),"--renew-ms",str(renew_ms),
        "--output",str(receipt)],cwd=args.repository_root)
    try:
        while not _STOP:
            rc=proc.poll()
            current=load(receipt)
            held=(current.get("state")=="LEASE_HELD" and current.get("model_sha")==args.model_sha
                  and current.get("owner")==owner)
            atomic(status,{"schema":SCHEMA,
                           "state":"LEASE_HELD" if held and rc is None else "LEASE_NOT_HELD",
                           "safe":bool(held and rc is None),
                           "paper_only":True,"authenticated_execution":False,
                           "real_order_submission":False,"model_sha":args.model_sha,
                           "owner":owner,"worker_returncode":rc,
                           "fencing_token":current.get("fencing_token"),
                           "lease_until_ms":current.get("lease_until_ms"),
                           "timestamp_ms":time.time_ns()//1_000_000})
            if rc is not None:return 3
            time.sleep(args.interval_seconds)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill()
    return 0

if __name__=="__main__":raise SystemExit(main())
