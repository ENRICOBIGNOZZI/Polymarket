#!/usr/bin/env python3
"""Long-running fail-closed multi-AZ lease supervisor for V7 PAPER."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path

from v7_multi_az_fencing_guard import validate

SCHEMA="polymarket_v7_multi_az_fencing_supervisor_v1"
_STOP=False

def stop(*_):
    global _STOP
    _STOP=True

def load(path:Path)->dict:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)

def kill(path:Path,sha:str,state:str)->None:
    atomic(path,{"schema":"polymarket_v7_runtime_failure_v1",
        "paper_only":True,"authenticated_execution":False,
        "model_sha":sha,"timestamp_ms":time.time_ns()//1_000_000,
        "reason":"MULTI_AZ_FENCING_LEASE_LOST","fencing_state":state})

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root",type=Path,required=True)
    ap.add_argument("--run-root",type=Path,required=True)
    ap.add_argument("--config",type=Path,required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--server-id",required=True)
    ap.add_argument("--run-id",required=True);ap.add_argument("--interval-seconds",type=float,default=1.0)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or not args.server_id or not args.run_id:
        raise SystemExit("invalid fencing identity")
    cfg=load(args.config)
    if (cfg.get("schema")!="polymarket_v7_failover_fencing_v1"
        or cfg.get("paper_only") is not True):
        raise SystemExit("invalid fencing config")
    lease_id=str(cfg.get("lease_id") or "")
    region=str(cfg.get("region") or "eu-west-2")
    lease_ms=int(cfg.get("lease_duration_ms") or 15000)
    renew_ms=int(cfg.get("renew_interval_ms") or 5000)
    minimum_remaining_ms=max(1000,min(lease_ms-renew_ms,renew_ms))
    if not(lease_id and 5000<=lease_ms<=120000 and 1000<=renew_ms<lease_ms
           and 0.1<=args.interval_seconds<=5.0):
        raise SystemExit("invalid fencing timing")
    owner=f"{args.run_id}:{args.server_id}"
    receipt=args.run_root/"control/az_fencing_lease.json"
    status=args.run_root/"control/fencing_supervisor_status.json"
    kill_marker=args.run_root/"control/KILL"
    env_name=str(cfg.get("table_name_environment") or "PM_V7_FENCING_TABLE")
    table=os.environ.get(env_name,"").strip()
    required=(
        cfg.get("enabled") is True
        or os.environ.get("PM_V7_MULTI_AZ_FENCING_REQUIRED","").lower() in {"1","true","yes"}
    )
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)

    if required and not table:
        state="REQUIRED_TABLE_MISSING"
        atomic(status,{"schema":SCHEMA,"state":state,"safe":False,
                       "paper_only":True,"authenticated_execution":False,
                       "real_order_submission":False,"model_sha":args.model_sha,
                       "owner":owner,"server_id":args.server_id})
        kill(kill_marker,args.model_sha,state)
        return 2

    if not table:
        while not _STOP:
            atomic(status,{"schema":SCHEMA,"state":"NOT_REQUIRED_SINGLE_NODE","safe":True,
                           "paper_only":True,"authenticated_execution":False,
                           "real_order_submission":False,"model_sha":args.model_sha,
                           "owner":owner,"server_id":args.server_id,
                           "timestamp_ms":time.time_ns()//1_000_000})
            time.sleep(args.interval_seconds)
        return 0

    lease=args.repository_root/"ops/v7_aws_fencing_lease.py"
    command=[
        sys.executable,str(lease),
        "--table",table,"--lease-id",lease_id,"--owner",owner,
        "--model-sha",args.model_sha,"--run-id",args.run_id,
        "--region",region,"--lease-ms",str(lease_ms),
        "--renew-ms",str(renew_ms),"--output",str(receipt),
    ]
    proc=subprocess.Popen(command,cwd=args.repository_root,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    startup_deadline=time.monotonic()+max(5.0,2*renew_ms/1000.0)
    try:
        while not _STOP:
            if proc.poll() is not None:
                state="LEASE_PROCESS_EXITED"
                atomic(status,{"schema":SCHEMA,"state":state,"safe":False,
                               "paper_only":True,"authenticated_execution":False,
                               "real_order_submission":False,"model_sha":args.model_sha,
                               "owner":owner,"server_id":args.server_id,
                               "exit_code":proc.returncode})
                kill(kill_marker,args.model_sha,state)
                return 3
            value=validate(load(receipt),args.model_sha,owner,minimum_remaining_ms,True)
            if value.get("safe") is True:
                atomic(status,{"schema":SCHEMA,**value,"server_id":args.server_id})
            elif time.monotonic()>=startup_deadline:
                state=str(value.get("state") or "UNSAFE_OR_STALE_LEASE")
                atomic(status,{"schema":SCHEMA,**value,"server_id":args.server_id})
                kill(kill_marker,args.model_sha,state)
                return 4
            time.sleep(args.interval_seconds)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
    return 0

if __name__=="__main__":raise SystemExit(main())
