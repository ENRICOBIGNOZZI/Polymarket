#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence

Q = Decimal("0.00001")

def finite(v: Any, d: float = math.nan) -> float:
    try: x=float(v)
    except (TypeError,ValueError,OverflowError): return d
    return x if math.isfinite(x) else d

@dataclass(frozen=True)
class FeeDetails:
    enabled: bool
    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    source: str = "unknown"

@dataclass(frozen=True)
class BookFill:
    requested_shares: float
    filled_shares: float
    raw_cash: float
    stressed_cash: float
    fee: float
    raw_vwap: float
    stressed_vwap: float
    all_in_unit_price: float
    slippage_cost: float
    complete: bool

def _bool(v: Any, d: bool=False) -> bool:
    if isinstance(v,bool): return v
    if isinstance(v,(int,float)): return bool(v)
    if isinstance(v,str):
        x=v.strip().lower()
        if x in {"true","1","yes"}: return True
        if x in {"false","0","no"}: return False
    return d

def _from_obj(o: dict[str,Any], source: str, hint: bool|None=None) -> FeeDetails|None:
    s=o.get("feeSchedule") if isinstance(o.get("feeSchedule"),dict) else None
    if s is None and isinstance(o.get("fd"),dict): s=o["fd"]
    if s is None and any(k in o for k in ("rate","feeRate","exponent","takerOnly")): s=o
    if s is None: return None
    r=finite(s.get("rate",s.get("feeRate")))
    if not math.isfinite(r): return None
    e=max(0.0,finite(s.get("exponent"),1.0))
    t=_bool(s.get("takerOnly"),True)
    enabled=(r>0.0) if hint is None else bool(hint and r>0.0)
    return FeeDetails(enabled,max(0.0,r),e,t,source)

def parse_fee_details(raw: dict[str,Any]) -> FeeDetails|None:
    if raw.get("feesEnabled") is False:
        return FeeDetails(False,0.0,1.0,True,"market:fees_disabled")
    hint=_bool(raw.get("feesEnabled"),False) if "feesEnabled" in raw else None
    return _from_obj(raw,"market:fee_schedule",hint)

def resolve_fee_details(raw: dict[str,Any], clob: str, request_json: Callable[...,Any],
                        fallback_rate: float=.07, fallback_exponent: float=1.0) -> FeeDetails:
    d=parse_fee_details(raw)
    if d is not None: return d
    cid=str(raw.get("conditionId") or raw.get("condition_id") or "")
    if cid:
        try:
            url=clob.rstrip("/")+"/clob-markets/"+urllib.parse.quote(cid,safe="")
            try: x=request_json(url,None,10)
            except TypeError: x=request_json(url)
            if isinstance(x,dict):
                if x.get("feesEnabled") is False:
                    return FeeDetails(False,0.0,1.0,True,"clob:fees_disabled")
                hint=_bool(x.get("feesEnabled"),True) if "feesEnabled" in x else None
                d=_from_obj(x,"clob:fee_schedule",hint)
                if d is not None: return d
        except Exception: pass
    r=max(0.0,finite(fallback_rate,.07)); e=max(0.0,finite(fallback_exponent,1.0))
    return FeeDetails(r>0.0,r,e,True,"fallback:conservative")

def round_fee_usdc(x: float) -> float:
    if not math.isfinite(x) or x<=0: return 0.0
    y=Decimal(str(x)).quantize(Q,rounding=ROUND_HALF_UP)
    return float(y) if y>=Q else 0.0

def fee_amount(shares: float, price: float, d: FeeDetails, taker: bool=True) -> float:
    if shares<=0 or not 0<price<1 or not d.enabled or d.rate<=0 or (d.taker_only and not taker): return 0.0
    return round_fee_usdc(shares*d.rate*(price*(1-price))**max(0.0,d.exponent))

def _levels(levels: Iterable[tuple[float,float]], buy: bool) -> list[tuple[float,float]]:
    out=[]
    for p,q in levels:
        p,q=finite(p),finite(q,0.0)
        if math.isfinite(p) and 0<p<1 and q>0: out.append((p,q))
    out.sort(key=lambda z:z[0],reverse=not buy)
    return out

def walk_book_for_shares(levels: Sequence[tuple[float,float]], shares: float, d: FeeDetails, *,
                         buy: bool, slippage_bps: float=0.0, require_full: bool=True) -> BookFill|None:
    target=max(0.0,finite(shares,0.0))
    if target<=0: return None
    rem=target; filled=raw=stress=fees=0.0; s=max(0.0,finite(slippage_bps,0.0))/1e4
    for p,q in _levels(levels,buy):
        take=min(rem,q)
        if take<=0: continue
        sp=min(.999999,p*(1+s)) if buy else max(.000001,p*(1-s))
        raw+=take*p; stress+=take*sp; fees+=fee_amount(take,sp,d,True); filled+=take; rem-=take
        if rem<=1e-10: break
    complete=rem<=max(1e-9,1e-8*target)
    if filled<=1e-12 or (require_full and not complete): return None
    rv=raw/filled; sv=stress/filled
    allin=(stress+fees)/filled if buy else (stress-fees)/filled
    return BookFill(target,filled,raw,stress,fees,rv,sv,allin,abs(stress-raw),complete)

def max_buy_for_cash(asks: Sequence[tuple[float,float]], cap: float, d: FeeDetails, *,
                     slippage_bps: float=0.0) -> BookFill|None:
    clean=_levels(asks,True); cap=max(0.0,finite(cap,0.0))
    if not clean or cap<=0: return None
    lo=0.0; hi=sum(q for _,q in clean); best=None
    for _ in range(36):
        mid=(lo+hi)/2
        f=walk_book_for_shares(clean,mid,d,buy=True,slippage_bps=slippage_bps,require_full=True) if mid>1e-12 else None
        if f is not None and f.stressed_cash+f.fee<=cap+1e-9: best=f; lo=mid
        else: hi=mid
    return best


SCRIPT_DIR=Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path: sys.path.insert(0,str(SCRIPT_DIR))
import v6_micro_taker as micro_legacy
import v6_hard_arb_paper as hard_legacy

def _micro_fee(m: Any, clob: str, cfg: dict[str,Any], sources: Counter[str]) -> FeeDetails:
    v6=cfg.get("v6") if isinstance(cfg.get("v6"),dict) else {}
    raw={"conditionId":m.condition}
    d=resolve_fee_details(raw,clob,micro_legacy.request_json,
                          float(v6.get("assumed_fee_rate",cfg.get("assumed_fee_rate",.07))),
                          float(v6.get("assumed_fee_exponent",cfg.get("assumed_fee_exponent",1.0))))
    sources[d.source]+=1
    return d

def _equity(cash: float, positions: dict[str,dict[str,Any]],
            current: dict[str,tuple[Any,Any,Any,Any]]) -> float:
    e=cash
    for mid,p in positions.items():
        cur=current.get(mid)
        if cur is None: e+=max(0.0,finite(p.get("cost"),0.0)); continue
        book=cur[1] if p.get("side")=="YES" else cur[2]
        bid=book.bid(); shares=max(0.0,finite(p.get("shares"),0.0))
        e+=shares*bid if math.isfinite(bid) else max(0.0,finite(p.get("cost"),0.0))
    return e

def _augment_positions(gamma: str, markets: list[Any], positions: dict[str,Any]) -> list[Any]:
    by={m.id for m in markets}
    for mid in positions:
        if mid in by: continue
        try:
            raw=micro_legacy.request_json(gamma.rstrip("/")+"/markets/"+mid)
            if isinstance(raw,dict):
                m=micro_legacy.Market(raw)
                if m.id and m.condition: markets.append(m); by.add(m.id)
        except Exception:
            pass
    return markets

def _micro_main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",type=Path,required=True); ap.add_argument("--run-dir",type=Path,required=True)
    ap.add_argument("--markets",type=int,default=250); ap.add_argument("--min-liquidity",type=float,default=25)
    ap.add_argument("--horizon-seconds",type=int,default=30); ap.add_argument("--max-target-staleness-seconds",type=int,default=10)
    ap.add_argument("--max-trade-usd",type=float,default=15); ap.add_argument("--min-edge",type=float,default=.0003)
    ap.add_argument("--slippage-bps",type=float,default=5); ap.add_argument("--max-positions",type=int,default=20)
    a=ap.parse_args()
    if a.horizon_seconds<=0 or a.max_target_staleness_seconds<0: raise SystemExit("invalid horizon/staleness")

    cfg=json.loads(a.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]
    start=float(cfg["starting_capital"]); now=int(time.time()); a.run_dir.mkdir(parents=True,exist_ok=True)
    sp=a.run_dir/"state.json"
    state=json.loads(sp.read_text()) if sp.exists() else {"cash":start,"peak":start,"killed":False,"positions":{},"samples":[]}
    cash=finite(state.get("cash"),start); peak=max(start,finite(state.get("peak"),start))
    positions=state.get("positions") if isinstance(state.get("positions"),dict) else {}
    samples=state.get("samples") if isinstance(state.get("samples"),list) else []
    realized_total=finite(state.get("realized_pnl_total"),0.0); failures=[]; sources=Counter()

    try:
        markets=_augment_positions(gamma,micro_legacy.discover(gamma,a.markets,a.min_liquidity),positions)
        books=micro_legacy.fetch_books(clob,markets)
    except Exception as exc:
        markets=[]; books={}; failures.append(f"market_data:{type(exc).__name__}:{exc}")

    current={}
    for m in markets:
        y,n=books.get(m.yes),books.get(m.no)
        if y and n:
            z=micro_legacy.features(y,n)
            if z: current[m.id]=(m,y,n,z)

    labels=micro_legacy.label_matured_samples(samples,now=now,horizon_seconds=a.horizon_seconds,
                                        max_target_staleness_seconds=a.max_target_staleness_seconds)
    beta=micro_legacy.solve_ridge(samples,1e-2); realized=0.0; partial_exits=0

    # Depth-aware exits. Public depth is walked level-by-level; slippage_bps is
    # an additional latency/adverse-selection stress, not a spread proxy.
    for mid,p in list(positions.items()):
        cur=current.get(mid)
        if cur is None: continue
        m,y,n,z=cur; side=p["side"]; book=y if side=="YES" else n
        pred=max(-2*z[2],min(2*z[2],sum(x*b for x,b in zip(beta,z[0])))); fair=z[1]+pred
        flip=(side=="YES" and fair<=z[1]) or (side=="NO" and fair>=z[1])
        if now-int(p["entry_ts"])<a.horizon_seconds and not flip: continue
        d=_micro_fee(m,clob,cfg,sources); old=max(0.0,finite(p.get("shares"),0.0))
        f=walk_book_for_shares(book.bids,old,d,buy=False,slippage_bps=a.slippage_bps,require_full=False)
        if f is None or f.filled_shares<=1e-12: continue
        sold=f.filled_shares; cb=max(0.0,finite(p.get("cost"),0.0)); alloc=cb*min(1.0,sold/max(old,1e-12))
        proceeds=f.stressed_cash-f.fee; pnl=proceeds-alloc; cash+=proceeds; realized+=pnl
        residual=max(0.0,old-sold); action="SELL" if residual<=1e-9 else "SELL_PARTIAL"
        if residual<=1e-9: del positions[mid]
        else: p["shares"]=residual; p["cost"]=max(0.0,cb-alloc); partial_exits+=1
        micro_legacy.append_csv(a.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl"],
                          {"timestamp":now,"market_id":mid,"slug":m.slug,"action":action,"side":side,
                           "shares":sold,"price":f.stressed_vwap,"fee":f.fee,"pnl":pnl})

    equity=_equity(cash,positions,current); peak=max(peak,equity)
    dd=max(0.0,1-equity/peak) if peak else 0.0; killed=bool(state.get("killed")) or dd>=float(cfg.get("max_drawdown",.15))
    max_market=float(cfg.get("max_market_fraction",.025)); max_event=float(cfg.get("max_event_fraction",.08))
    max_gross=float(cfg.get("max_gross_fraction",.45)); max_dd=float(cfg.get("max_drawdown",.15))
    gross=sum(max(0.0,finite(p.get("cost"),0.0)) for p in positions.values())
    event_cost=Counter()
    for p in positions.values(): event_cost[str(p.get("event") or "")]+=max(0.0,finite(p.get("cost"),0.0))

    ranked=[]
    if not killed:
        for mid,(m,y,n,z) in current.items():
            if mid in positions: continue
            pred=max(-2*z[2],min(2*z[2],sum(x*b for x,b in zip(beta,z[0])))); q=max(.001,min(.999,z[1]+pred))
            for side,book,fair in (("YES",y,q),("NO",n,1-q)):
                ask=book.ask()
                if math.isfinite(ask) and fair-ask>a.min_edge: ranked.append((fair-ask,m,side,fair))
    ranked.sort(reverse=True,key=lambda r:r[0]); signals=opened=depth_rej=0; best_edge=0.0

    for _,m,side,fair in ranked:
        if killed or len(positions)>=a.max_positions or m.id in positions: break
        _,y,n,_=current[m.id]; book=y if side=="YES" else n; equity=_equity(cash,positions,current)
        current_dd=max(0.0,peak-equity); dd_room=max(0.0,max_dd*peak-current_dd-gross)
        room=min(a.max_trade_usd,max(0.0,max_market*equity),max(0.0,max_event*equity-event_cost[m.event]),
                 max(0.0,max_gross*equity-gross),dd_room,cash)
        if room<=0: continue
        d=_micro_fee(m,clob,cfg,sources); f=max_buy_for_cash(book.asks,room,d,slippage_bps=a.slippage_bps)
        if f is None or f.filled_shares+1e-12<book.min_order: depth_rej+=1; continue
        edge=fair-f.all_in_unit_price; best_edge=max(best_edge,edge)
        if edge<=a.min_edge: continue
        signals+=1; cost=f.stressed_cash+f.fee
        if cost>cash+1e-9: continue
        cash-=cost; gross+=cost; event_cost[m.event]+=cost
        positions[m.id]={"side":side,"shares":f.filled_shares,"entry_price":f.stressed_vwap,"entry_ts":now,
                         "cost":cost,"entry_fee":f.fee,"event":m.event,"fee_source":d.source}
        opened+=1
        micro_legacy.append_csv(a.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl"],
                          {"timestamp":now,"market_id":m.id,"slug":m.slug,"action":"BUY","side":side,
                           "shares":f.filled_shares,"price":f.stressed_vwap,"fee":f.fee,"pnl":0.0})

    for mid,(_,_,_,z) in current.items(): samples.append({"ts":now,"market_id":mid,"mid":z[1],"x":z[0],"y":None})
    if len(samples)>20000: samples=samples[-20000:]
    realized_total+=realized; equity=_equity(cash,positions,current); peak=max(peak,equity)
    dd=max(0.0,1-equity/peak) if peak else 0.0; killed=killed or dd>=max_dd
    out={"timestamp":now,"cash":cash,"equity":equity,"peak":peak,"drawdown":dd,"killed":killed,
         "positions":positions,"samples":samples,"markets":len(markets),"books":len(books),"signals":signals,"opened":opened,
         "best_edge":best_edge,"realized_pnl_last_tick":realized,"realized_pnl_total":realized_total,
         "label_stats_last_tick":labels,"target_staleness_max_seconds":labels.get("max_target_staleness_seconds"),
         "partial_exits_last_tick":partial_exits,"depth_rejections_last_tick":depth_rej,
         "fee_sources_last_tick":dict(sources),"failures":failures,"paper_only":True,
         "execution_model":"depth_vwap_plus_latency_stress"}
    micro_legacy.atomic_json(sp,out); micro_legacy.atomic_json(a.run_dir/"status.json",out)
    micro_legacy.append_csv(a.run_dir/"equity.csv",["timestamp","cash","equity","drawdown","positions","realized_pnl_total","killed"],
                      {"timestamp":now,"cash":cash,"equity":equity,"drawdown":dd,"positions":len(positions),
                       "realized_pnl_total":realized_total,"killed":int(killed)})
    print(f"micro_exec markets={len(markets)} signals={signals} opened={opened} positions={len(positions)} "
          f"best_edge={best_edge:.8f} realized={realized:.8f} depth_rejections={depth_rej} equity={equity:.6f}")
    return 0


def _parse_book(raw: dict[str,Any]) -> dict[str,Any]|None:
    token=str(raw.get("asset_id") or ""); bids=[]; asks=[]
    for key,out in (("bids",bids),("asks",asks)):
        for z in raw.get(key,[]):
            if isinstance(z,dict):
                p,q=finite(z.get("price")),finite(z.get("size"),0.0)
                if math.isfinite(p) and 0<p<1 and q>0: out.append((p,q))
    bids.sort(reverse=True); asks.sort()
    if not token or not asks: return None
    return {"token":token,"bids":bids,"asks":asks,"ask":asks[0][0],"size":asks[0][1],
            "min_order":max(1.0,finite(raw.get("min_order_size"),1.0)),
            "ask_depth":sum(q for _,q in asks)}

def books(clob: str, tokens: list[str]) -> dict[str,dict[str,Any]]:
    out={}
    for i in range(0,len(tokens),80):
        raw=hard_legacy.get_json(clob.rstrip("/")+"/books",[{"token_id":t} for t in tokens[i:i+80]])
        for z in raw if isinstance(raw,list) else []:
            if isinstance(z,dict):
                b=_parse_book(z)
                if b: out[b["token"]]=b
    return out

def _hard_fee(raw: dict[str,Any], clob: str, cfg: dict[str,Any], sources: Counter[str]) -> FeeDetails:
    v6=cfg.get("v6") if isinstance(cfg.get("v6"),dict) else {}
    d=resolve_fee_details(raw,clob,hard_legacy.get_json,
                          float(v6.get("assumed_fee_rate",cfg.get("assumed_fee_rate",.07))),
                          float(v6.get("assumed_fee_exponent",cfg.get("assumed_fee_exponent",1.0))))
    sources[d.source]+=1; return d

def _plan(live: dict[str,dict[str,Any]], tokens: list[str], fees: dict[str,FeeDetails],
          shares: float, slip: float) -> tuple[float,float,list[dict[str,Any]]]|None:
    cost=raw=0.0; fills=[]
    for t in tokens:
        b=live.get(t)
        if b is None: return None
        f=walk_book_for_shares(b["asks"],shares,fees[t],buy=True,slippage_bps=slip,require_full=True)
        if f is None: return None
        c=f.stressed_cash+f.fee; cost+=c; raw+=f.raw_cash
        fills.append({"token":t,"shares":f.filled_shares,"raw_vwap":f.raw_vwap,"price":f.stressed_vwap,
                      "fee":f.fee,"cost":c,"slippage":f.slippage_cost})
    return cost,raw,fills

def _size(live: dict[str,dict[str,Any]], tokens: list[str], fees: dict[str,FeeDetails],
          min_order: float, max_shares: float, room: float, edge_gate: float, slip: float):
    if room<=0 or max_shares+1e-12<min_order: return None
    p=_plan(live,tokens,fees,min_order,slip)
    if p is None or p[0]>room+1e-9 or 1-p[0]/min_order<=edge_gate: return None
    lo,hi=min_order,max_shares; best=(lo,*p)
    for _ in range(34):
        x=(lo+hi)/2; p=_plan(live,tokens,fees,x,slip)
        if p and p[0]<=room+1e-9 and 1-p[0]/x>edge_gate: best=(x,*p); lo=x
        else: hi=x
    return best

def _unwind(clob: str, legs: list[dict[str,Any]], fees: dict[str,FeeDetails], slip: float):
    residual=[]; received=pnl=0.0
    for leg in reversed(legs):
        t=str(leg["token"]); q=max(0.0,finite(leg.get("shares"),0.0)); cb=max(0.0,finite(leg.get("cost"),0.0))
        try: b=books(clob,[t]).get(t)
        except Exception: b=None
        if not b or not b.get("bids"): residual.append(dict(leg)); continue
        f=walk_book_for_shares(b["bids"],q,fees[t],buy=False,slippage_bps=slip,require_full=False)
        if f is None: residual.append(dict(leg)); continue
        sold=f.filled_shares; alloc=cb*min(1.0,sold/max(q,1e-12)); proceeds=f.stressed_cash-f.fee
        received+=proceeds; pnl+=proceeds-alloc; rem=max(0.0,q-sold)
        if rem>1e-9:
            r=dict(leg); r["shares"]=rem; r["cost"]=max(0.0,cb-alloc); residual.append(r)
    residual.reverse(); return residual,received,pnl

def _abort_mark(clob: str, aborting: dict[str,Any]) -> float:
    value=0.0
    for bundle in aborting.values():
        for leg in bundle.get("legs",[]):
            t=str(leg.get("token") or ""); q=max(0.0,finite(leg.get("shares"),0.0))
            try:
                b=books(clob,[t]).get(t); bid=b["bids"][0][0] if b and b.get("bids") else math.nan
            except Exception: bid=math.nan
            if math.isfinite(bid): value+=q*bid
    return value

def _hard_main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",type=Path,required=True); ap.add_argument("--run-dir",type=Path,required=True)
    ap.add_argument("--markets",type=int,default=500); ap.add_argument("--min-liquidity",type=float,default=10)
    ap.add_argument("--max-events",type=int,default=80); ap.add_argument("--min-edge",type=float,default=.0002)
    ap.add_argument("--max-trade-usd",type=float,default=60); ap.add_argument("--slippage-bps",type=float,default=5)
    ap.add_argument("--leg-latency-ms",type=int,default=100); a=ap.parse_args()
    cfg=json.loads(a.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; start=float(cfg["starting_capital"])
    maxdd=float(cfg.get("max_drawdown",.15)); maxgross=float(cfg.get("max_gross_fraction",.45)); maxevent=float(cfg.get("max_event_fraction",.08))
    now=int(time.time()); a.run_dir.mkdir(parents=True,exist_ok=True); sp=a.run_dir/"state.json"
    state=json.loads(sp.read_text()) if sp.exists() else {"cash":start,"peak":start,"killed":False,"bundles":{},"aborting":{},"realized_pnl":0.0}
    cash=finite(state.get("cash"),start); peak=max(start,finite(state.get("peak"),start)); realized=finite(state.get("realized_pnl"),0.0)
    openb=state.get("bundles") if isinstance(state.get("bundles"),dict) else {}
    aborting=state.get("aborting") if isinstance(state.get("aborting"),dict) else {}
    failures=[]; sources=Counter(); scanned=positive=entered=seq_aborts=0; best_edge=0.0

    for eid,b in list(openb.items()):
        try:
            e=hard_legacy.get_json(f"{gamma.rstrip('/')}/events/{eid}")
            if isinstance(e,dict) and e.get("closed"):
                payout=float(b["shares"]); cash+=payout; pnl=payout-float(b["cost"]); realized+=pnl
                hard_legacy.append_csv(a.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","pnl"],
                                  {"timestamp":now,"event_id":eid,"action":"SETTLE","shares":b["shares"],"cost":b["cost"],
                                   "payout":payout,"net_edge":b["net_edge"],"pnl":pnl}); del openb[eid]
        except Exception as exc:
            if len(failures)<20: failures.append(f"settle:{eid}:{type(exc).__name__}")

    for eid,b in list(aborting.items()):
        legs=b.get("legs") if isinstance(b.get("legs"),list) else []; fmap={}
        for leg in legs:
            d=_hard_fee(leg.get("market") if isinstance(leg.get("market"),dict) else {},clob,cfg,sources); fmap[str(leg["token"])]=d
        residual,recv,pnl=_unwind(clob,legs,fmap,a.slippage_bps); cash+=recv; realized+=pnl
        if residual: b["legs"]=residual; b["cost"]=sum(finite(x.get("cost"),0.0) for x in residual)
        else: del aborting[eid]

    locked=sum(float(b["cost"]) for b in openb.values()); abort_cost=sum(float(b.get("cost") or 0) for b in aborting.values())
    locked_profit=sum(float(b["shares"])-float(b["cost"]) for b in openb.values()); equity=cash+locked+(_abort_mark(clob,aborting) if aborting else 0.0)
    peak=max(peak,equity); dd=max(0.0,1-equity/peak) if peak else 0.0; killed=bool(state.get("killed")) or dd>=maxdd

    if not killed and not aborting:
        try: event_ids=hard_legacy.discover_event_ids(gamma,a.markets,a.min_liquidity,a.max_events)
        except Exception as exc: event_ids=[]; failures.append(f"discover:{type(exc).__name__}:{exc}")
        for eid in event_ids:
            if eid in openb: continue
            try:
                markets=hard_legacy.event_spec(gamma,eid)
                if markets is None: continue
                tokens=[]; fmap={}; mtoken={}
                for m in markets:
                    yes,_=hard_legacy.market_tokens(m) or ("","")
                    if not yes: raise RuntimeError("missing_yes_token")
                    tokens.append(yes); mtoken[yes]=m; fmap[yes]=_hard_fee(m,clob,cfg,sources)
                live=books(clob,tokens)
                if any(t not in live for t in tokens): continue
                scanned+=1
                # Initial all-or-none FOK sizing uses full displayed depth; the
                # actual paper fills below are sequential, not atomic snapshot fills.
                min_size=min(float(live[t]["ask_depth"]) for t in tokens); min_order=max(float(live[t]["min_order"]) for t in tokens)
                eq=max(1.0,equity); room=min(a.max_trade_usd,max(0.0,maxgross*eq-locked-abort_cost),max(0.0,maxevent*eq),cash)
                p=_plan(live,tokens,fmap,min_order,a.slippage_bps) if min_size+1e-12>=min_order else None
                if p:
                    cost_per_share=p[0]/min_order; e=1-cost_per_share; best_edge=max(best_edge,e); positive+=int(e>0)
                sized=_size(live,tokens,fmap,min_order,min_size,room,a.min_edge,a.slippage_bps)
                if sized is None: continue
                shares=sized[0]; order=sorted(tokens,key=lambda t:float(live[t]["ask_depth"]))
                filled=[]; execution_cost=0.0; fail=""
                for i,t in enumerate(order):
                    if i and a.leg_latency_ms>0: time.sleep(a.leg_latency_ms/1000.0)
                    remain=order[i:]; fresh=books(clob,remain)
                    if any(x not in fresh for x in remain): fail="book_missing"; break
                    rp=_plan(fresh,remain,fmap,shares,a.slippage_bps)
                    if rp is None: fail="fok_depth"; break
                    guarantee=1-(execution_cost+rp[0])/shares
                    if guarantee<=a.min_edge: fail="edge_revalidation"; break
                    cf=next(x for x in rp[2] if x["token"]==t); c=float(cf["cost"])
                    if c>cash+1e-9: fail="capital"; break
                    cash-=c; execution_cost+=c; filled.append({**cf,"market":mtoken[t]})
                    hard_legacy.append_csv(a.run_dir/"leg_fills.csv",["timestamp","event_id","action","token","shares","price","fee","cost","detail"],
                                      {"timestamp":int(time.time()),"event_id":eid,"action":"BUY_LEG_FOK","token":t,"shares":shares,
                                       "price":cf["price"],"fee":cf["fee"],"cost":c,"detail":f"leg={i+1}/{len(order)} edge={guarantee:.8f}"})
                if fail:
                    seq_aborts+=1; residual,recv,pnl=_unwind(clob,filled,fmap,a.slippage_bps); cash+=recv; realized+=pnl
                    if residual: aborting[eid]={"shares":shares,"cost":sum(float(x["cost"]) for x in residual),"legs":residual,"reason":fail,"opened_ts":now}
                    if aborting: break
                    continue
                edge=1-execution_cost/shares
                if edge<=a.min_edge: raise RuntimeError("post_execution_edge")
                openb[eid]={"shares":shares,"cost":execution_cost,"net_edge":edge,"raw_edge":1-sum(float(x["raw_vwap"]) for x in filled),
                            "opened_ts":now,"legs":len(markets),"leg_latency_ms":a.leg_latency_ms}
                locked+=execution_cost; locked_profit+=shares-execution_cost; equity=cash+locked; entered+=1; best_edge=max(best_edge,edge)
                hard_legacy.append_csv(a.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","pnl"],
                                  {"timestamp":int(time.time()),"event_id":eid,"action":"BUY_COMPLETE_YES_SET_SEQUENTIAL",
                                   "shares":shares,"cost":execution_cost,"payout":shares,"net_edge":edge,"pnl":0.0})
            except Exception as exc:
                if len(failures)<20: failures.append(f"event:{eid}:{type(exc).__name__}:{exc}")

    locked=sum(float(b["cost"]) for b in openb.values()); abort_cost=sum(float(b.get("cost") or 0) for b in aborting.values())
    locked_profit=sum(float(b["shares"])-float(b["cost"]) for b in openb.values()); equity=cash+locked+(_abort_mark(clob,aborting) if aborting else 0.0)
    peak=max(peak,equity); dd=max(0.0,1-equity/peak) if peak else 0.0; killed=killed or dd>=maxdd
    out={"timestamp":int(time.time()),"cash":cash,"equity":equity,"peak":peak,"drawdown":dd,"killed":killed,
         "bundles":openb,"aborting":aborting,"gross_exposure":locked+abort_cost,"open_positions":len(openb),
         "aborting_bundles":len(aborting),"realized_pnl":realized,"locked_expected_profit":locked_profit,
         "scanned_events":scanned,"positive_candidates":positive,"entered":entered,"sequential_aborts":seq_aborts,
         "best_edge":best_edge,"failures":failures,"fee_sources_last_tick":dict(sources),"paper_only":True,
         "atomic_snapshot_assumption":False,"sequential_leg_revalidation":True,"leg_latency_ms":a.leg_latency_ms,
         "marking":"complete_sets_cost_basis_abort_legs_bid_mark"}
    hard_legacy.atomic_json(sp,out); hard_legacy.atomic_json(a.run_dir/"status.json",out)
    print(f"hard_exec scanned={scanned} positive={positive} entered={entered} sequential_aborts={seq_aborts} aborting={len(aborting)} best_edge={best_edge:.8f} realized={realized:.6f}")
    return 0


def _self_test() -> int:
    disabled=parse_fee_details({"feesEnabled":False})
    assert disabled is not None and not disabled.enabled and fee_amount(100,.5,disabled,True)==0.0
    active=FeeDetails(True,.07,1.0,True,"selftest")
    assert fee_amount(100,.5,active,True)==1.75
    assert fee_amount(100,.5,active,False)==0.0
    fill=walk_book_for_shares([(.50,5),(.60,5)],8,active,buy=True,slippage_bps=5,require_full=True)
    assert fill is not None and fill.complete and fill.raw_vwap>.50 and fill.all_in_unit_price>fill.stressed_vwap
    partial=walk_book_for_shares([(.49,2)],5,active,buy=False,slippage_bps=5,require_full=False)
    assert partial is not None and partial.filled_shares==2 and not partial.complete
    print("v6_queue_filter_self_test=ok")
    return 0

def main() -> int:
    if len(sys.argv)<2 or sys.argv[1] in {"-h","--help"}:
        print("v6_queue_filter.py {micro|hard|self-test} [engine arguments]")
        return 0
    command=sys.argv[1]
    sys.argv=[sys.argv[0],*sys.argv[2:]]
    if command=="micro": return _micro_main()
    if command=="hard": return _hard_main()
    if command=="self-test": return _self_test()
    raise SystemExit(f"unknown V6 execution command: {command}")

if __name__=="__main__": raise SystemExit(main())
