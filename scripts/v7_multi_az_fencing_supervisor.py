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

def load(path:Path)->dict:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root",type=Path,required=True)
    ap.add_argument("--run-root",type=Path,required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--server-id",required=True)
    ap.add_argument("--owner-id",required=True);ap.add_argument("--lease-id",default="polymarket-v7-paper-single-writer")
    ap.add_argument("--region",default="eu-west-2")
    ap.add_argument("--lease-ms",type=int,default=15000);ap.add_argument("--renew-ms",type=int,default=5000)
    ap.add_argument("--minimum-remaining-ms",type=int,default=5000)
    ap.add_argument("--poll-ms",type=int,default=1000)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or not args.server_id or not args.owner_id:
        raise SystemExit("invalid fencing identity")
    if not (5000<=args.lease_ms<=120000 and 1000<=args.renew_ms<args.lease_ms
            and 500<=args.poll_ms<=5000 and 1000<=args.minimum_remaining_ms<args.lease_ms):
        raise SystemExit("invalid fencing timing")
    receipt=args.run_root/"control/fencing_receipt.json"
    status=args.run_root/"control/fencing_supervisor_status.json"
    kill_marker=args.run_root/"control/KILL"
    table=os.environ.get("PM_V7_FENCING_TABLE","").strip()
    required=os.environ.get("PM_V7_MULTI_AZ_FENCING_REQUIRED","").lower() in {"1","true","yes"}
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)

    if required and not table:
        state="REQUIRED_TABLE_MISSING"
        atomic(status,{"schema":SCHEMA,"state":state,"safe":False,
                       "paper_only":True,"model_sha":args.model_sha})
        kill(kill_marker,args.model_sha,state)
        return 2

    if not table:
        while not _STOP:
            atomic(status,{"schema":SCHEMA,"state":"NOT_REQUIRED_SINGLE_NODE","safe":True,
                           "paper_only":True,"authenticated_execution":False,
                           "real_order_submission":False,"model_sha":args.model_sha,
                           "server_id":args.server_id,"timestamp_ms":time.time_ns()//1_000_000})
            time.sleep(args.poll_ms/1000.0)
        return 0

    lease=args.repository_root/"ops/v7_aws_fencing_lease.py"
    command=[
        sys.executable,str(lease),
        "--table",table,"--lease-id",args.lease_id,"--owner",args.owner_id,
        "--model-sha",args.model_sha,"--run-id",args.owner_id,
        "--region",args.region,"--lease-ms",str(args.lease_ms),
        "--renew-ms",str(args.renew_ms),"--output",str(receipt),
    ]
    proc=subprocess.Popen(
        command,cwd=args.repository_root,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    startup_deadline=time.monotonic()+max(5.0,args.renew_ms/1000.0*2)
    try:
        while not _STOP:
            if proc.poll() is not None:
                state="LEASE_PROCESS_EXITED"
                atomic(status,{"schema":SCHEMA,"state":state,"safe":False,
                               "paper_only":True,"model_sha":args.model_sha,
                               "exit_code":proc.returncode})
                kill(kill_marker,args.model_sha,state)
                return 3
            value=validate(
                load(receipt),args.model_sha,args.owner_id,
                args.minimum_remaining_ms,True)
            if value.get("safe") is True:
                atomic(status,{"schema":SCHEMA,**value,"server_id":args.server_id})
            elif time.monotonic()>=startup_deadline:
                state=str(value.get("state") or "UNSAFE_OR_STALE_LEASE")
                atomic(status,{"schema":SCHEMA,**value,"server_id":args.server_id})
                kill(kill_marker,args.model_sha,state)
                return 4
            time.sleep(args.poll_ms/1000.0)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
    return 0

if __name__=="__main__":raise SystemExit(main())
