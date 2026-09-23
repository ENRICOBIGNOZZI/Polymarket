#!/usr/bin/env python3
"""Public CLOB clock-offset guard for the London PAPER runtime."""
from __future__ import annotations
import argparse,json,time,urllib.request
from pathlib import Path

SCHEMA="polymarket_v7_clock_guard_v1"

def _quantized_offset_seconds(server_timestamp:float,local_midpoint:float,
                              resolution_seconds:float)->float:
    """Signed minimum clock offset consistent with a quantized server timestamp."""
    if resolution_seconds <= 0:
        return server_timestamp-local_midpoint
    lower=server_timestamp
    upper=server_timestamp+resolution_seconds
    if local_midpoint < lower:
        return lower-local_midpoint
    if local_midpoint >= upper:
        return upper-local_midpoint
    return 0.0


def server_time(url:str,timeout:float,proxy_url:str|None=None)->tuple[float,float,float,float]:
    target=url.rstrip("/")+"/time"
    request=urllib.request.Request(
        target,
        headers={"User-Agent":"polymarket-v7-clock-guard/1","Accept":"application/json"},
        method="GET",
    )
    opener=(urllib.request.build_opener(
        urllib.request.ProxyHandler({"https":proxy_url})
    ) if proxy_url else urllib.request.build_opener())
    t0=time.time()
    with opener.open(request,timeout=timeout) as r:
        raw=r.read().decode().strip()
    t1=time.time()
    try:value=json.loads(raw)
    except json.JSONDecodeError:value=raw
    if isinstance(value,dict):
        value=value.get("timestamp") or value.get("time") or value.get("server_time")
    if value is None:
        raise ValueError("CLOB /time response missing timestamp")
    x=float(value)
    # The CLOB /time endpoint currently returns integer Unix seconds. Treat
    # that as a one-second interval [x,x+1), not as an exact point at x. If a
    # millisecond timestamp is returned later, preserve its 1 ms resolution.
    if x>1e12:
        x/=1000.0
        resolution_s=0.001
    elif isinstance(value,int) or (isinstance(value,str) and value.strip().lstrip("-").isdigit()):
        resolution_s=1.0
    else:
        resolution_s=0.0
    midpoint=(t0+t1)/2.0
    raw_offset_s=x-midpoint
    offset_s=_quantized_offset_seconds(x,midpoint,resolution_s)
    return offset_s,t1-t0,resolution_s,raw_offset_s

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--https-proxy",default="")
    ap.add_argument("--maximum-absolute-offset-ms",type=float,default=50.0)
    ap.add_argument("--timeout-seconds",type=float,default=2.0);ap.add_argument("--interval-seconds",type=float,default=5.0)
    ap.add_argument("--fail-after-consecutive-unsafe",type=int,default=3)
    args=ap.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    consecutive_unsafe=0
    while True:
        now_ms=time.time_ns()//1_000_000
        error=""
        try:
            offset_s,rtt_s,resolution_s,raw_offset_s=server_time(
                args.clob_url,args.timeout_seconds,args.https_proxy or None)
            offset_ms=offset_s*1000.0;rtt_ms=rtt_s*1000.0
            server_resolution_ms=resolution_s*1000.0
            raw_offset_ms=raw_offset_s*1000.0
            safe=abs(offset_ms)<=args.maximum_absolute_offset_ms
            state="OK" if safe else "CLOCK_OFFSET_UNSAFE"
        except Exception as exc:
            offset_ms=None;rtt_ms=None;server_resolution_ms=None;raw_offset_ms=None
            safe=False;state="CLOCK_SOURCE_UNAVAILABLE"
            error=(f"{type(exc).__name__}:{exc}")[:256]
        value={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
               "real_order_submission":False,"model_sha":args.model_sha,"timestamp_ms":now_ms,
               "state":state,"safe":safe,"offset_ms":offset_ms,"rtt_ms":rtt_ms,
               "raw_offset_ms":raw_offset_ms,
               "server_time_resolution_ms":server_resolution_ms,
               "error":error,"https_proxy_configured":bool(args.https_proxy),
               "maximum_absolute_offset_ms":args.maximum_absolute_offset_ms}
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        consecutive_unsafe = 0 if safe else consecutive_unsafe + 1
        if consecutive_unsafe >= args.fail_after_consecutive_unsafe:
            return 2
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
