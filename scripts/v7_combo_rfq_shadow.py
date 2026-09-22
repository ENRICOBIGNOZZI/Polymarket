#!/usr/bin/env python3
"""Zero-authority Combo RFQ no-arbitrage shadow.

Consumes captured RFQ_REQUEST messages.  It never authenticates, signs, quotes,
confirms or cancels.  Exact hedge bounds use the conjunction semantics of Combo
legs:
  - sell Combo YES: buy the cheapest constituent leg;
  - sell Combo NO: buy all constituent complements;
  - buy Combo YES: Combo YES + all complements guarantees >= 1;
  - buy Combo NO: Combo NO + any constituent leg guarantees >= 1.
Unknown fees/books fail closed.
"""
from __future__ import annotations
import argparse,json,math,time,urllib.parse,urllib.request
from pathlib import Path
from typing import Any
from v7_pure_arb_economics import raw_fee_per_share

SCHEMA="polymarket_v7_combo_rfq_shadow_v1"

def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def get_json(url:str,timeout:float)->dict[str,Any]|None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-rfq-shadow"}),timeout=timeout) as r:
            v=json.load(r)
    except Exception:return None
    return v if isinstance(v,dict) else None

def book(base:str,token:str,timeout:float)->dict[str,float]|None:
    v=get_json(base.rstrip("/")+"/book?"+urllib.parse.urlencode({"token_id":token}),timeout)
    if not v:return None
    def lv(rows):
        out=[]
        for x in rows or []:
            if not isinstance(x,dict):continue
            try:p=float(x["price"]);q=float(x["size"])
            except Exception:continue
            if 0<p<1 and q>0 and math.isfinite(p) and math.isfinite(q):out.append((p,q))
        return out
    bids,asks=lv(v.get("bids")),lv(v.get("asks"))
    if not bids or not asks:return None
    b=max(bids);a=min(asks)
    return {"bid":b[0],"bid_q":b[1],"ask":a[0],"ask_q":a[1]}

def fee_rate(base:str,token:str,timeout:float)->float|None:
    v=get_json(base.rstrip("/")+"/fee-rate?"+urllib.parse.urlencode({"token_id":token}),timeout)
    try:bps=int(v["base_fee"]) if v else -1
    except Exception:return None
    return bps/10_000.0 if 0<=bps<=10_000 else None

def taker_cost(price:float,shares:float,rate:float)->float:
    fee=raw_fee_per_share(price,rate,1.0)
    return shares*(price+fee) if math.isfinite(fee) else math.nan

def evaluate(req:dict[str,Any],catalog:dict[str,Any],args:argparse.Namespace)->dict[str,Any]:
    now=int(req.get("receive_wall_ms") or time.time_ns()//1_000_000)
    rid=str(req.get("rfq_id") or "");direction=str(req.get("direction") or "").upper()
    side=str(req.get("side") or "").upper();legs=[str(x) for x in req.get("leg_position_ids") or []]
    try:deadline=int(req.get("submission_deadline") or 0)
    except Exception:deadline=0
    base={"rfq_id":rid,"direction":direction,"side":side,"leg_count":len(legs),
          "receive_wall_ms":now,"submission_deadline":deadline,
          "quote_budget_ms":deadline-now if deadline else None,"state":"CENSORED"}
    if not rid or direction not in {"BUY","SELL"} or side not in {"YES","NO"} or not legs:
        base["state"]="INVALID_REQUEST";return base
    if deadline<=now:base["state"]="SUBMISSION_WINDOW_CLOSED";return base
    index=catalog.get("position_index") if isinstance(catalog.get("position_index"),dict) else {}
    resolved=[]
    for pid in legs:
        meta=index.get(pid)
        if not isinstance(meta,dict):base["state"]="UNKNOWN_LEG";return base
        comp=str(meta.get("complement_position_id") or "")
        b=book(args.clob_url,pid,args.timeout_seconds);cb=book(args.clob_url,comp,args.timeout_seconds)
        r=fee_rate(args.clob_url,pid,args.timeout_seconds);cr=fee_rate(args.clob_url,comp,args.timeout_seconds)
        if b is None or cb is None or r is None or cr is None:
            base["state"]="CENSORED_HEDGE_DATA";return base
        resolved.append({"position_id":pid,"complement_position_id":comp,
                         "book":b,"complement_book":cb,"fee_rate":r,"complement_fee_rate":cr})
    q=args.reference_shares
    if side=="YES" and direction=="BUY":
        # requester buys YES -> maker sells YES; one constituent YES superhedges.
        hedges=[taker_cost(x["book"]["ask"],q,x["fee_rate"]) for x in resolved]
        hedge=min(hedges);bound=hedge/q
        relation="SHORT_COMBO_YES_PLUS_LONG_ONE_LEG_YES_NONNEGATIVE"
    elif side=="NO" and direction=="BUY":
        hedge=sum(taker_cost(x["complement_book"]["ask"],q,x["complement_fee_rate"]) for x in resolved)
        bound=hedge/q;relation="SHORT_COMBO_NO_PLUS_LONG_ALL_LEG_COMPLEMENTS_NONNEGATIVE"
    elif side=="YES" and direction=="SELL":
        hedge=sum(taker_cost(x["complement_book"]["ask"],q,x["complement_fee_rate"]) for x in resolved)
        bound=1.0-hedge/q;relation="LONG_COMBO_YES_PLUS_ALL_LEG_COMPLEMENTS_GUARANTEES_ONE"
    else:
        hedges=[taker_cost(x["book"]["ask"],q,x["fee_rate"]) for x in resolved]
        bound=1.0-min(hedges)/q;relation="LONG_COMBO_NO_PLUS_ONE_LEG_YES_GUARANTEES_ONE"
    base.update(state="PRICED_EXACT_BOUND",reference_quote_bound=bound,
                exact_relation=relation,reserve_per_share=args.reserve_per_share,
                actionable_bound=(bound+args.reserve_per_share if direction=="BUY" else bound-args.reserve_per_share),
                legs=resolved)
    return base

def iter_tail(path:Path,max_rows:int):
    try:lines=path.read_text(encoding="utf-8").splitlines()[-max_rows:]
    except OSError:return []
    out=[]
    for raw in lines:
        try:v=json.loads(raw)
        except json.JSONDecodeError:continue
        if isinstance(v,dict) and v.get("type")=="RFQ_REQUEST":out.append(v)
    return out

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--catalog",type=Path,required=True)
    ap.add_argument("--rfq-tape",type=Path,required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--timeout-seconds",type=float,default=1.0);ap.add_argument("--reference-shares",type=float,default=5.0)
    ap.add_argument("--reserve-per-share",type=float,default=0.001);ap.add_argument("--maximum-rows",type=int,default=10000)
    args=ap.parse_args()
    catalog=load(args.catalog)
    safe=(catalog.get("schema")=="polymarket_v7_combo_market_source_v1"
          and catalog.get("paper_only") is True and catalog.get("authenticated_execution") is False
          and catalog.get("real_order_submission") is False and catalog.get("model_sha")==args.model_sha)
    rows=[evaluate(r,catalog,args) for r in iter_tail(args.rfq_tape,args.maximum_rows)] if safe else []
    value={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
           "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RFF_SHADOW",
           "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000,
           "catalog_safe":safe,"requests":len(rows),
           "priced":sum(r.get("state")=="PRICED_EXACT_BOUND" for r in rows),
           "rows":rows[-1000:]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix(args.output.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
    return 0

if __name__=="__main__":raise SystemExit(main())
