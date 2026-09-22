#!/usr/bin/env python3
"""Public, fail-closed Polymarket venue-mode observer.

Reads the official public Polymarket status page. It never submits orders and
never grants execution authority. Classification is intentionally conservative:
only an explicit operational CLOB + CLOB Websocket surface yields NORMAL.
Unknown HTML, stale fetches, errors, maintenance or degraded wording yield
DEGRADED/PAUSED. Explicit post-only/cancel-only wording wins when present.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import time
import urllib.request
from typing import Any

SCHEMA="polymarket_v7_public_venue_mode_source_v1"
DEFAULT_URL="https://status.polymarket.com/en-us"


def atomic_json(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)


def normalize_page(raw:str)->str:
    raw=re.sub(r"(?is)<script.*?</script>|<style.*?</style>"," ",raw)
    raw=re.sub(r"(?s)<[^>]+>"," ",raw)
    raw=html.unescape(raw)
    return re.sub(r"\s+"," ",raw).strip()


def current_surface(text:str)->str:
    lower=text.lower()
    cut=len(text)
    for marker in ("recent notices","notice history"):
        i=lower.find(marker)
        if i>=0:cut=min(cut,i)
    return text[:cut]


def classify(text:str)->tuple[str,str]:
    surface=current_surface(text)
    low=surface.lower()

    if "cancel-only" in low or "cancel only" in low:
        return "CANCEL_ONLY","PUBLIC_STATUS_CANCEL_ONLY"
    if "post-only" in low or "post only" in low:
        return "POST_ONLY","PUBLIC_STATUS_POST_ONLY"
    if ("predictions trading api (clob)" in low
        and ("under maintenance" in low or "trading paused" in low or "paused" in low)):
        return "PAUSED","PUBLIC_STATUS_CLOB_PAUSED"
    if any(x in low for x in (
        "predictions trading api (clob) - degraded",
        "predictions trading api (clob) degraded",
        "partial outage","major outage","degraded performance",
    )):
        return "DEGRADED","PUBLIC_STATUS_DEGRADED"

    clob_operational=bool(re.search(
        r"predictions trading api \(clob\)\s*(?:-|:)?\s*operational",low))
    ws_operational=bool(re.search(
        r"clob websocket\s*(?:-|:)?\s*operational",low))
    all_operational="all systems operational" in low
    if all_operational and clob_operational and ws_operational:
        return "NORMAL","PUBLIC_STATUS_EXPLICIT_OPERATIONAL"
    return "DEGRADED","PUBLIC_STATUS_UNCLASSIFIED"


def fetch(url:str,timeout:float)->tuple[str,int]:
    req=urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-venue-observer/1"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        raw=resp.read(2_000_000).decode("utf-8","replace")
        code=int(getattr(resp,"status",200))
    return raw,code


def build(url:str,timeout:float,now_ms:int)->dict[str,Any]:
    value={
        "schema":SCHEMA,"version":1,"timestamp_ms":now_ms,
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"real_capital_at_risk":False,
        "execution_authority":"ZERO_AUTHORITY_PUBLIC_OBSERVER",
        "source_url":url,"mode":"DEGRADED","reason":"FETCH_NOT_ATTEMPTED",
        "http_status":None,"classification_fail_closed":True,
    }
    try:
        raw,code=fetch(url,timeout)
        text=normalize_page(raw)
        mode,reason=classify(text)
        value.update(mode=mode,reason=reason,http_status=code,
                     page_bytes=len(raw.encode("utf-8")),
                     surface_sha256=__import__("hashlib").sha256(
                         current_surface(text).encode()).hexdigest())
    except Exception as exc:
        value["reason"]=f"FETCH_ERROR:{type(exc).__name__}"
    return value


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--url",default=DEFAULT_URL)
    ap.add_argument("--timeout-seconds",type=float,default=3.0)
    ap.add_argument("--interval-ms",type=int,default=1000)
    args=ap.parse_args()
    if not(.2<=args.timeout_seconds<=30 and 100<=args.interval_ms<=60_000):
        raise SystemExit("invalid timing")
    while True:
        atomic_json(args.output,build(
            args.url,args.timeout_seconds,time.time_ns()//1_000_000))
        time.sleep(args.interval_ms/1000.0)


if __name__=="__main__":
    raise SystemExit(main())
