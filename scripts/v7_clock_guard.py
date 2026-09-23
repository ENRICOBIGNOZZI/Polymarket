#!/usr/bin/env python3
"""Public CLOB clock-offset guard for the London PAPER runtime."""
from __future__ import annotations
import argparse,json,time,urllib.request
from pathlib import Path

SCHEMA="polymarket_v7_clock_guard_v1"

def server_time(url:str,timeout:float)->float:
    t0=time.time()
    with urllib.request.urlopen(url.rstrip("/")+"/time",timeout=timeout) as r:
        raw=r.read().decode().strip()
    t1=time.time()
    try:value=json.loads(raw)
    except json.JSONDecodeError:value=raw
    if isinstance(value,dict):
        value=value.get("timestamp") or value.get("time") or value.get("server_time")
    x=float(value)
    if x>1e12:x/=1000.0
    # midpoint corrects half RTT under symmetric path assumption
    return x-(t0+t1)/2.0,t1-t0

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--maximum-absolute-offset-ms",type=float,default=50.0)
    ap.add_argument("--timeout-seconds",type=float,default=2.0);ap.add_argument("--interval-seconds",type=float,default=5.0)
    ap.add_argument("--fail-after-consecutive-unsafe",type=int,default=3)
    args=ap.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    consecutive_unsafe=0
    while True:
        now_ms=time.time_ns()//1_000_000
        error_type=None;error_message=None
        try:
            offset_s,rtt_s=server_time(args.clob_url,args.timeout_seconds)
            offset_ms=offset_s*1000.0;rtt_ms=rtt_s*1000.0
            safe=abs(offset_ms)<=args.maximum_absolute_offset_ms
            state="OK" if safe else "CLOCK_OFFSET_UNSAFE"
        except Exception as exc:
            offset_ms=None;rtt_ms=None;safe=False;state="CLOCK_SOURCE_UNAVAILABLE"
            error_type=type(exc).__name__
            error_message=str(exc)[:500]
        value={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
               "real_order_submission":False,"model_sha":args.model_sha,"timestamp_ms":now_ms,
               "state":state,"safe":safe,"offset_ms":offset_ms,"rtt_ms":rtt_ms,
               "maximum_absolute_offset_ms":args.maximum_absolute_offset_ms,
               "error_type":error_type,"error":error_message}
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        consecutive_unsafe = 0 if safe else consecutive_unsafe + 1
        if consecutive_unsafe >= args.fail_after_consecutive_unsafe:
            return 2
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
