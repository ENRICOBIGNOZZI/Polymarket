#!/usr/bin/env python3
"""Zero-authority PAPER scanner for deterministic cross-market duplicate arbitrage.

A relation is admissible only when two distinct open markets have the same
asset, horizon, contract family, settlement-semantic hash and exact time window.
For such payoff-identical binaries, YES_A + NO_B and NO_A + YES_B each redeem
to exactly one dollar in every state. Books are fetched read-only from CLOB;
missing fees/books fail closed.

This is discovery/PAPER capacity only. It cannot submit orders or promote itself.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any

STATUS_SCHEMA = "polymarket_v7_cross_market_exact_arb_status_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not (math.isfinite(price) and 0 < price < 1 and 0 <= rate <= 1 and exponent >= 0):
        return math.nan
    return rate*(price*(1-price))**exponent if rate else 0.0


def fee_params(row: dict[str, Any]) -> tuple[float,float] | None:
    fee=row.get("fee_schedule")
    if isinstance(fee,dict):
        try:
            rate=float(fee["rate"]); exponent=float(fee.get("exponent",1.0))
        except (KeyError,TypeError,ValueError):
            return None
        if math.isfinite(rate) and 0<=rate<=1 and math.isfinite(exponent) and exponent>=0:
            return rate,exponent
    if row.get("fees_enabled_explicit") is True and row.get("fees_enabled") is False:
        return 0.0,1.0
    return None


def exact_identity(row: dict[str, Any]) -> tuple[Any,...] | None:
    try:
        start=int(row.get("window_start_unix") or 0)
        close=int(row.get("close_timestamp_unix") or 0)
    except (TypeError,ValueError):
        return None
    semantic=str(row.get("settlement_semantic_hash") or "")
    asset=str(row.get("asset") or "")
    horizon=str(row.get("horizon") or "")
    family=str(row.get("contract_family") or "")
    if not (asset and horizon and family and len(semantic)==64 and start>0 and close>start):
        return None
    return asset,horizon,family,semantic,start,close


def token_map(row: dict[str,Any]) -> dict[str,str] | None:
    tokens=[str(x) for x in row.get("clob_token_ids") or []]
    outcomes=[str(x).strip().upper() for x in row.get("outcomes") or []]
    if len(tokens)!=2 or len(outcomes)<2 or not all(tokens) or tokens[0]==tokens[1]:
        return None
    result={}
    for i,outcome in enumerate(outcomes[:2]):
        if outcome in {"YES","UP"}: result["YES"]=tokens[i]
        elif outcome in {"NO","DOWN"}: result["NO"]=tokens[i]
    return result if set(result)=={"YES","NO"} else None


def fetch_book(base: str, token: str, timeout: float) -> dict[str,float] | None:
    url=base.rstrip("/")+"/book?"+urllib.parse.urlencode({"token_id":token})
    req=urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-cross-market-shadow"})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            value=json.load(resp)
    except (OSError,TimeoutError,json.JSONDecodeError):
        return None
    if not isinstance(value,dict): return None
    bids=value.get("bids") if isinstance(value.get("bids"),list) else []
    asks=value.get("asks") if isinstance(value.get("asks"),list) else []
    def levels(rows):
        out=[]
        for x in rows:
            if not isinstance(x,dict): continue
            try:p=float(x["price"]);q=float(x["size"])
            except (KeyError,TypeError,ValueError): continue
            if math.isfinite(p) and math.isfinite(q) and 0<p<1 and q>0: out.append((p,q))
        return out
    bb=levels(bids); aa=levels(asks)
    if not bb or not aa: return None
    bid=max(bb,key=lambda x:x[0]); ask=min(aa,key=lambda x:x[0])
    if not bid[0] < ask[0]: return None
    return {"bid":bid[0],"bid_q":bid[1],"ask":ask[0],"ask_q":ask[1]}


def scan(args: argparse.Namespace) -> dict[str,Any]:
    universe=load(args.universe)
    safe=(universe.get("paper_only") is True
          and universe.get("authenticated_execution") is False
          and universe.get("real_order_submission") is False
          and universe.get("execution_authority") is False
          and universe.get("model_sha")==args.model_sha)
    if not safe:
        return {"schema":STATUS_SCHEMA,"state":"UNIVERSE_INVALID","paper_only":True,
                "authenticated_execution":False,"real_order_submission":False,
                "real_capital_at_risk":False,"model_sha":args.model_sha,
                "timestamp_ms":time.time_ns()//1_000_000,"identity_groups":0,
                "pairs_checked":0,"opportunities":[]}

    now=int(time.time())
    groups: defaultdict[tuple[Any,...],list[dict[str,Any]]]=defaultdict(list)
    for row in universe.get("markets") or []:
        if not isinstance(row,dict) or row.get("active") is not True or row.get("closed") is True                 or row.get("accepting_orders") is not True:
            continue
        identity=exact_identity(row)
        mapping=token_map(row)
        fees=fee_params(row)
        if identity is None or mapping is None or fees is None: continue
        if not (identity[-2] <= now < identity[-1]): continue
        groups[identity].append({**row,"_tokens":mapping,"_fees":fees})

    duplicate_groups=[rows for rows in groups.values() if len(rows)>1]
    opportunities=[]; pairs_checked=0; books={}
    for rows in duplicate_groups:
        rows=sorted(rows,key=lambda x:str(x.get("market_id") or ""))
        for i in range(len(rows)):
            for j in range(i+1,len(rows)):
                a,b=rows[i],rows[j]; pairs_checked+=1
                needed=[a["_tokens"]["YES"],a["_tokens"]["NO"],b["_tokens"]["YES"],b["_tokens"]["NO"]]
                ok=True
                for token in needed:
                    if token not in books:
                        books[token]=fetch_book(args.clob_url,token,args.timeout_seconds)
                    if books[token] is None: ok=False
                if not ok: continue
                combos=[
                    ("YES_A_PLUS_NO_B",a["_tokens"]["YES"],b["_tokens"]["NO"],a,b),
                    ("NO_A_PLUS_YES_B",a["_tokens"]["NO"],b["_tokens"]["YES"],a,b),
                ]
                for kind,t1,t2,m1,m2 in combos:
                    x,y=books[t1],books[t2]
                    r1,e1=m1["_fees"]; r2,e2=m2["_fees"]
                    f1=fee_per_share(x["ask"],r1,e1); f2=fee_per_share(y["ask"],r2,e2)
                    if not (math.isfinite(f1) and math.isfinite(f2)): continue
                    raw=1-x["ask"]-y["ask"]
                    edge=raw-f1-f2-args.reserve_per_share
                    shares=min(x["ask_q"],y["ask_q"],args.maximum_shares)
                    if edge>args.minimum_locked_edge_per_share and shares>=args.minimum_shares:
                        opportunities.append({
                            "kind":kind,
                            "market_a":str(a.get("market_id") or ""),
                            "market_b":str(b.get("market_id") or ""),
                            "asset":str(a.get("asset") or ""),
                            "horizon":str(a.get("horizon") or ""),
                            "identity":{
                                "contract_family":str(a.get("contract_family") or ""),
                                "settlement_semantic_hash":str(a.get("settlement_semantic_hash") or ""),
                                "window_start_unix":int(a.get("window_start_unix") or 0),
                                "close_timestamp_unix":int(a.get("close_timestamp_unix") or 0),
                            },
                            "token_1":t1,"token_2":t2,
                            "ask_1":x["ask"],"ask_2":y["ask"],
                            "raw_edge_per_share":raw,
                            "locked_edge_per_share":edge,
                            "executable_shares":shares,
                            "locked_pnl_capacity":shares*edge,
                        })
    return {
        "schema":STATUS_SCHEMA,"state":"COLLECTING","paper_only":True,
        "authenticated_execution":False,"real_order_submission":False,
        "real_capital_at_risk":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion":False,"model_sha":args.model_sha,
        "timestamp_ms":time.time_ns()//1_000_000,
        "identity_rule":"EXACT_ASSET_HORIZON_FAMILY_SETTLEMENT_HASH_WINDOW",
        "identity_groups":len(duplicate_groups),"pairs_checked":pairs_checked,
        "opportunities_count":len(opportunities),
        "locked_pnl_capacity":sum(float(x["locked_pnl_capacity"]) for x in opportunities),
        "opportunities":sorted(opportunities,key=lambda x:x["locked_pnl_capacity"],reverse=True)[:100],
    }


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--reserve-per-share",type=float,default=0.0005)
    ap.add_argument("--minimum-locked-edge-per-share",type=float,default=0.0005)
    ap.add_argument("--minimum-shares",type=float,default=1.0)
    ap.add_argument("--maximum-shares",type=float,default=1000.0)
    ap.add_argument("--timeout-seconds",type=float,default=2.0)
    ap.add_argument("--interval-seconds",type=float,default=1.0)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not (0<=args.reserve_per_share<1 and 0<=args.minimum_locked_edge_per_share<1
            and 0<args.minimum_shares<=args.maximum_shares and .1<=args.timeout_seconds<=10
            and .1<=args.interval_seconds<=60):
        raise SystemExit("invalid arguments")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        value=scan(args)
        tmp=args.output.with_name(args.output.name+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
        tmp.replace(args.output)
        time.sleep(args.interval_seconds)


if __name__=="__main__":
    raise SystemExit(main())
