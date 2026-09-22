#!/usr/bin/env python3
"""Zero-authority Combo RFQ no-arbitrage shadow.

Consumes captured RFQ_REQUEST messages.  It never authenticates, signs, quotes,
confirms or cancels.  Exact hedge bounds use the conjunction semantics of Combo
legs:
  - requester BUY YES: maker sells Combo YES and buys one constituent leg;
  - requester SELL YES: maker buys Combo YES and buys all complements.
Current RFQ side is YES-only. BUY requests are notional-sized; SELL requests
are share-sized. Unknown depth/fees fail closed.
Unknown fees/books fail closed.
"""
from __future__ import annotations
import argparse,json,math,os,time
from collections import deque
from pathlib import Path
from typing import Any
from v7_pure_arb_economics import raw_fee_per_share
from v7_clob_public_batch import bbo as parse_bbo, fetch_books

SCHEMA="polymarket_v7_combo_rfq_shadow_v1"

def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def batch_bbos(base:str,tokens:list[str],timeout:float)->dict[str,dict[str,float]|None]:
    raw=fetch_books(base,tokens,timeout,chunk_size=50,user_agent="polymarket-v7-rfq-shadow")
    return {token:parse_bbo(raw.get(token)) for token in dict.fromkeys(tokens)}

def taker_cost(price:float,shares:float,rate:float)->float:
    fee=raw_fee_per_share(price,rate,1.0)
    return shares*(price+fee) if math.isfinite(fee) else math.nan

def _requested_size(req:dict[str,Any],direction:str)->tuple[str,float]|None:
    row=req.get("requested_size")
    if not isinstance(row,dict):return None
    unit=str(row.get("unit") or "").lower()
    raw=row.get("value_e6")
    try:value_e6=int(str(raw))
    except (TypeError,ValueError):return None
    if value_e6<=0:return None
    expected="notional" if direction=="BUY" else "shares"
    if unit!=expected:return None
    return unit,value_e6/1_000_000.0


def evaluate(req:dict[str,Any],catalog:dict[str,Any],args:argparse.Namespace)->dict[str,Any]:
    now=int(req.get("receive_wall_ms") or time.time_ns()//1_000_000)
    rid=str(req.get("rfq_id") or "");direction=str(req.get("direction") or "").upper()
    side=str(req.get("side") or "").upper();legs=[str(x) for x in req.get("leg_position_ids") or []]
    try:deadline=int(req.get("submission_deadline") or 0)
    except Exception:deadline=0
    requested=_requested_size(req,direction)
    base={"rfq_id":rid,"direction":direction,"side":side,"leg_count":len(legs),
          "receive_wall_ms":now,"submission_deadline":deadline,
          "quote_budget_ms":deadline-now if deadline else None,"state":"CENSORED"}
    if not rid or direction not in {"BUY","SELL"} or side!="YES" or not legs or requested is None:
        base["state"]="INVALID_OR_UNSUPPORTED_REQUEST";return base
    if deadline<=now:base["state"]="SUBMISSION_WINDOW_CLOSED";return base
    unit,requested_value=requested
    base["requested_size_unit"]=unit;base["requested_size_value"]=requested_value
    index=catalog.get("position_index") if isinstance(catalog.get("position_index"),dict) else {}
    metas=[];tokens=[]
    for pid in legs:
        meta=index.get(pid)
        if not isinstance(meta,dict):base["state"]="UNKNOWN_LEG";return base
        comp=str(meta.get("complement_position_id") or "")
        if not comp:base["state"]="UNKNOWN_COMPLEMENT";return base
        metas.append((pid,comp));tokens.extend((pid,comp))
    books=batch_bbos(args.clob_url,tokens,args.timeout_seconds)
    resolved=[]
    for pid,comp in metas:
        b=books.get(pid);cb=books.get(comp)
        meta=index.get(pid) or {};cmeta=index.get(comp) or {}
        r=meta.get("fee_rate");cr=cmeta.get("fee_rate")
        try:r=float(r);cr=float(cr)
        except (TypeError,ValueError):
            base["state"]="CENSORED_FEE_METADATA";return base
        if b is None or cb is None or not(0<=r<=1 and 0<=cr<=1):
            base["state"]="CENSORED_HEDGE_DATA";return base
        resolved.append({"position_id":pid,"complement_position_id":comp,
                         "book":b,"complement_book":cb,"fee_rate":r,"complement_fee_rate":cr})
    if direction=="BUY":
        # User buys YES; maker sells YES. Safe quote lower bound is the cheapest
        # constituent YES hedge. BUY RFQs specify pUSD notional, so quote size is
        # floor(notional_e6 * 1e6 / price_e6). Evaluate at the conservative
        # lower quote bound, which maximizes required shares.
        per_share=[]
        for x in resolved:
            unit_cost=taker_cost(x["book"]["ask"],1.0,x["fee_rate"])
            if math.isfinite(unit_cost):per_share.append((unit_cost,x))
        if not per_share:base["state"]="CENSORED_HEDGE_DATA";return base
        hedge_unit,chosen=min(per_share,key=lambda z:z[0])
        quote=max(0.0,hedge_unit+args.reserve_per_share)
        price_e6=max(1,int(math.ceil(quote*1_000_000.0-1e-12)))
        shares_e6=(int(round(requested_value*1_000_000.0))*1_000_000)//price_e6
        q=shares_e6/1_000_000.0
        if q<=0 or chosen["book"]["ask_q"]+1e-12<q:
            base["state"]="CENSORED_BBO_DEPTH";return base
        bound=hedge_unit;actionable=price_e6/1_000_000.0
        relation="SHORT_COMBO_YES_PLUS_LONG_ONE_LEG_YES_NONNEGATIVE"
        base["hedge_position_id"]=chosen["position_id"]
    else:
        # User sells exact YES shares; maker buys YES. Buying all constituent
        # complements guarantees at least 1 pUSD together with Combo YES.
        q=requested_value
        if any(x["complement_book"]["ask_q"]+1e-12<q for x in resolved):
            base["state"]="CENSORED_BBO_DEPTH";return base
        hedge=sum(taker_cost(x["complement_book"]["ask"],1.0,x["complement_fee_rate"])
                  for x in resolved)
        bound=1.0-hedge
        actionable=max(0.0,bound-args.reserve_per_share)
        relation="LONG_COMBO_YES_PLUS_ALL_LEG_COMPLEMENTS_GUARANTEES_ONE"
    feasible=(0.0<actionable<1.0 and q>0)
    base.update(
        state="PRICED_EXACT_BOUND" if feasible else "NO_FEASIBLE_QUOTE_DOMAIN",
        reference_quote_bound=bound,exact_relation=relation,
        reserve_per_share=args.reserve_per_share,actionable_bound=actionable,
        quote_domain_feasible=feasible,quote_size_shares=q,
        sizing_semantics=("BUY_NOTIONAL_FLOOR_AT_QUOTE_PRICE" if direction=="BUY"
                          else "SELL_EXACT_SHARES"),
        hedge_depth_semantics="BBO_ONLY_FAIL_CLOSED_IF_INSUFFICIENT",
        legs=resolved)
    return base

class RfqTail:
    def __init__(self,path:Path,*,start_at_end:bool=False):
        self.path=path;self.handle=None;self.start_at_end_pending=bool(start_at_end)
    def poll(self)->list[dict[str,Any]]:
        out=[]
        for _ in range(2):
            if self.handle is None:
                try:self.handle=self.path.open("rb")
                except OSError:return out
                if self.start_at_end_pending:
                    self.handle.seek(0,os.SEEK_END);self.start_at_end_pending=False
            while True:
                pos=self.handle.tell();raw=self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(pos);break
                try:v=json.loads(raw)
                except (json.JSONDecodeError,UnicodeDecodeError):continue
                if isinstance(v,dict) and v.get("type")=="RFQ_REQUEST":out.append(v)
            try:
                old,cur=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(cur.st_dev,cur.st_ino):
                    if cur.st_size<self.handle.tell():self.handle.seek(0)
                    return out
            except OSError:return out
            self.handle.close();self.handle=None
        return out


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--catalog",type=Path,required=True)
    ap.add_argument("--rfq-tape",type=Path,required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--timeout-seconds",type=float,default=1.0)
    ap.add_argument("--reserve-per-share",type=float,default=0.001);ap.add_argument("--maximum-rows",type=int,default=1000)
    ap.add_argument("--seen-rfq-limit",type=int,default=100000)
    ap.add_argument("--interval-seconds",type=float,default=0.05)
    args=ap.parse_args()
    if not (100<=args.maximum_rows<=100000 and 1000<=args.seen_rfq_limit<=1000000
            and .01<=args.interval_seconds<=60):
        raise SystemExit("invalid bounds")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    try:resumed=args.output.exists() and args.output.stat().st_size>0
    except OSError:resumed=False
    tail=RfqTail(args.rfq_tape,start_at_end=resumed)
    rows=deque(maxlen=args.maximum_rows);seen=set();seen_order=deque()
    total_requests=0;total_priced=0;total_infeasible=0
    while True:
        catalog=load(args.catalog)
        safe=(catalog.get("schema")=="polymarket_v7_combo_market_source_v1"
              and catalog.get("paper_only") is True and catalog.get("authenticated_execution") is False
              and catalog.get("real_order_submission") is False and catalog.get("model_sha")==args.model_sha)
        new=tail.poll()
        if safe:
            for req in new:
                rid=str(req.get("rfq_id") or "")
                if not rid or rid in seen:continue
                while len(seen_order)>=args.seen_rfq_limit:
                    old=seen_order.popleft();seen.discard(old)
                seen.add(rid);seen_order.append(rid)
                row=evaluate(req,catalog,args);rows.append(row);total_requests+=1
                total_priced+=row.get("state")=="PRICED_EXACT_BOUND"
                total_infeasible+=row.get("state")=="NO_FEASIBLE_QUOTE_DOMAIN"
        value={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
               "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RFQ_SHADOW",
               "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000,
               "catalog_safe":safe,"requests_since_start":total_requests,
               "priced_since_start":total_priced,"infeasible_since_start":total_infeasible,
               "status_window_size":len(rows),"seen_rfq_count":len(seen),
               "restart_policy":"FOLLOW_NEW_RFQS_ONLY" if resumed else "INITIAL_TAPE_DRAIN",
               "rows":list(rows)}
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
