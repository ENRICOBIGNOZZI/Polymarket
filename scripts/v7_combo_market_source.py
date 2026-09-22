#!/usr/bin/env python3
"""Public zero-authority Combo market catalog collector."""
from __future__ import annotations
import argparse,json,time,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_combo_market_source_v1"

_FEE_CACHE:dict[str,tuple[float,float]]={}

def fee_rate(base:str,token:str,timeout:float)->float|None:
    now=time.monotonic()
    cached=_FEE_CACHE.get(token)
    if cached is not None and now-cached[1]<=600.0:return cached[0]
    url=base.rstrip("/")+"/fee-rate?"+urllib.parse.urlencode({"token_id":token})
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-combo-catalog"}),
            timeout=timeout) as resp:
            value=json.load(resp)
        bps=int(value["base_fee"])
    except Exception:
        return None
    if not 0<=bps<=10_000:return None
    rate=bps/10_000.0
    _FEE_CACHE[token]=(rate,now)
    return rate

def prewarm_fees(base:str,tokens:list[str],timeout:float,workers:int)->dict[str,float]:
    unique=list(dict.fromkeys(x for x in tokens if x))
    out={}
    with ThreadPoolExecutor(max_workers=max(1,min(workers,32))) as pool:
        fut={pool.submit(fee_rate,base,token,timeout):token for token in unique}
        for f in as_completed(fut):
            token=fut[f]
            try:rate=f.result()
            except Exception:rate=None
            if rate is not None:out[token]=rate
    return out

def fetch_page(base:str,limit:int,cursor:str|None,timeout:float)->dict[str,Any]:
    q={"limit":str(limit)}
    if cursor:q["cursor"]=cursor
    url=base.rstrip("/")+"/v1/rfq/combo-markets?"+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-combo-shadow"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        value=json.load(resp)
    if not isinstance(value,dict) or not isinstance(value.get("markets"),list):
        raise ValueError("invalid combo market response")
    return value

def normalize(row:dict[str,Any])->dict[str,Any]|None:
    ids=[str(x) for x in row.get("position_ids") or []]
    outcomes=[str(x).upper() for x in row.get("outcomes") or []]
    prices=[str(x) for x in row.get("outcome_prices") or []]
    if len(ids)!=2 or len(outcomes)!=2 or len(prices)!=2 or not all(ids):return None
    return {
        "market_id":str(row.get("id") or ""),
        "condition_id":str(row.get("condition_id") or ""),
        "position_ids":ids,"outcomes":outcomes,"outcome_prices":prices,
        "slug":str(row.get("slug") or ""),"title":str(row.get("title") or ""),
        "volume":float(row.get("volume") or 0.0),
        "tags":[str(x) for x in row.get("tags") or []],
    }

def collect(args:argparse.Namespace)->dict[str,Any]:
    cursor=None;rows=[];pages=0;seen=set()
    while pages<args.maximum_pages:
        page=fetch_page(args.base_url,args.page_size,cursor,args.timeout_seconds);pages+=1
        for raw in page["markets"]:
            if not isinstance(raw,dict):continue
            row=normalize(raw)
            if row and row["market_id"] and row["market_id"] not in seen:
                seen.add(row["market_id"]);rows.append(row)
        cursor=page.get("next_cursor")
        if cursor is None:break
        cursor=str(cursor)
    all_positions=[pid for row in rows for pid in row["position_ids"]]
    fee_rates=prewarm_fees(
        args.clob_url,all_positions,args.fee_timeout_seconds,args.fee_workers)
    pos={}
    for row in rows:
        for idx,pid in enumerate(row["position_ids"]):
            comp=row["position_ids"][1-idx]
            pos[pid]={
                "market_id":row["market_id"],"condition_id":row["condition_id"],
                "outcome":row["outcomes"][idx],
                "complement_position_id":comp,
                "fee_rate":fee_rates.get(pid),
                "fee_verified":pid in fee_rates,
            }
    return {
        "schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_PUBLIC_DATA",
        "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000,
        "pages":pages,"markets":rows,"position_index":pos,
        "fee_rates_verified":len(fee_rates),"position_ids":len(all_positions),
    }

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--base-url",default="https://combos-rfq-api.polymarket.com")
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--fee-timeout-seconds",type=float,default=0.5)
    ap.add_argument("--fee-workers",type=int,default=16)
    ap.add_argument("--page-size",type=int,default=100);ap.add_argument("--maximum-pages",type=int,default=50)
    ap.add_argument("--timeout-seconds",type=float,default=3.0);ap.add_argument("--interval-seconds",type=float,default=15.0)
    args=ap.parse_args()
    if len(args.model_sha)!=40:raise SystemExit("invalid sha")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        try:value=collect(args)
        except Exception as exc:
            value={"schema":SCHEMA,"state":"ERROR","paper_only":True,"authenticated_execution":False,
                   "real_order_submission":False,"model_sha":args.model_sha,
                   "timestamp_ms":time.time_ns()//1_000_000,"error":type(exc).__name__}
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
