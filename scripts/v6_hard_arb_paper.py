#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try: x=float(value)
    except (TypeError,ValueError,OverflowError): return default
    return x if math.isfinite(x) else default


def arr(value: Any) -> list[Any]:
    if isinstance(value,list): return value
    if isinstance(value,str):
        try:
            x=json.loads(value); return x if isinstance(x,list) else []
        except json.JSONDecodeError: return []
    return []


def get_json(url: str, payload: Any|None=None, timeout: int=20) -> Any:
    data=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(url,data=data,headers={"User-Agent":"polymarket-v6-hard-arb-paper/1","Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode())


def fee_ps(px: float, rate: float, exp: float) -> float:
    if not 0<px<1 or rate<=0:return 0.0
    return rate*(px*(1-px))**max(0.0,exp)


def atomic_json(path:Path,obj:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(obj,indent=2,sort_keys=True)+"\n"); os.replace(tmp,path)


def append_csv(path:Path,fields:list[str],row:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True); exists=path.exists() and path.stat().st_size>0
    with path.open("a",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=fields)
        if not exists:w.writeheader()
        w.writerow({k:row.get(k,"") for k in fields})


def market_tokens(raw:dict)->tuple[str,str]|None:
    ids=[str(x) for x in arr(raw.get("clobTokenIds"))]; outcomes=[str(x).lower() for x in arr(raw.get("outcomes"))]
    if len(ids)<2:return None
    yi,ni=0,1
    for i,x in enumerate(outcomes[:len(ids)]):
        if x=="yes":yi=i
        elif x=="no":ni=i
    return ids[yi],ids[ni]


def discover_event_ids(gamma:str,limit:int,min_liq:float)->list[str]:
    ids=[]; offset=0
    while len(ids)<limit and offset<5000:
        qs=urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        raw=get_json(gamma+"/markets?"+qs); batch=raw if isinstance(raw,list) else raw.get("markets",[]) if isinstance(raw,dict) else []
        if not batch:break
        for m in batch:
            if not isinstance(m,dict) or not m.get("negRisk"):continue
            if finite(m.get("liquidityNum"),finite(m.get("liquidity"),0))<min_liq:continue
            eid=str(m.get("eventId") or "")
            ev=m.get("events")
            if not eid and isinstance(ev,list) and ev and isinstance(ev[0],dict):eid=str(ev[0].get("id") or "")
            if eid and eid not in ids:ids.append(eid)
            if len(ids)>=limit:break
        if len(batch)<100:break
        offset+=100
    return ids


def event_spec(gamma:str,event_id:str)->tuple[list[dict],bool]|None:
    e=get_json(f"{gamma}/events/{event_id}")
    if not isinstance(e,dict) or not e.get("negRisk") or e.get("negRiskAugmented"):return None
    ms=e.get("markets")
    if not isinstance(ms,list) or len(ms)<2:return None
    clean=[]
    for m in ms:
        if not isinstance(m,dict) or market_tokens(m) is None:return None
        if m.get("closed") or m.get("active") is False or m.get("enableOrderBook") is False:return None
        clean.append(m)
    return clean,bool(e.get("closed",False))


def books(clob:str,tokens:list[str])->dict[str,dict]:
    out={}
    for i in range(0,len(tokens),80):
        raw=get_json(clob+"/books",[{"token_id":x} for x in tokens[i:i+80]])
        for b in raw if isinstance(raw,list) else []:
            if not isinstance(b,dict):continue
            token=str(b.get("asset_id") or ""); asks=[]
            for z in b.get("asks",[]):
                if isinstance(z,dict):
                    p,q=finite(z.get("price")),finite(z.get("size"),0)
                    if math.isfinite(p) and 0<p<1 and q>0:asks.append((p,q))
            asks.sort()
            if token and asks:out[token]={"ask":asks[0][0],"size":asks[0][1],"min_order":max(1.0,finite(b.get("min_order_size"),1.0))}
    return out


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path,required=True); ap.add_argument("--run-dir",type=Path,required=True); ap.add_argument("--markets",type=int,default=500); ap.add_argument("--min-liquidity",type=float,default=10); ap.add_argument("--max-events",type=int,default=80); ap.add_argument("--min-edge",type=float,default=.0002); ap.add_argument("--max-trade-usd",type=float,default=60); ap.add_argument("--slippage-bps",type=float,default=5); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; starting=float(cfg["starting_capital"]); max_dd=float(cfg.get("max_drawdown",.15)); max_gross=float(cfg.get("max_gross_fraction",.45)); max_event=float(cfg.get("max_event_fraction",.08)); now=int(time.time()); args.run_dir.mkdir(parents=True,exist_ok=True)
    sp=args.run_dir/"state.json"; state=json.loads(sp.read_text()) if sp.exists() else {"cash":starting,"peak":starting,"killed":False,"bundles":{},"realized_pnl":0.0}; cash=finite(state.get("cash"),starting); peak=max(starting,finite(state.get("peak"),starting)); open_b=state.get("bundles") if isinstance(state.get("bundles"),dict) else {}; realized=finite(state.get("realized_pnl"),0.0); failures=[]; scanned=0; candidates=0; entered=0; best_edge=0.0

    # Guaranteed complete-set payout is $1 per set. Closed-event settlement can be
    # recognized without knowing which outcome won, because exactly one YES pays 1.
    for eid,b in list(open_b.items()):
        try:
            e=get_json(f"{gamma}/events/{eid}")
            if isinstance(e,dict) and e.get("closed"):
                payout=float(b["shares"]); cash+=payout; pnl=payout-float(b["cost"]); realized+=pnl
                append_csv(args.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","pnl"],{"timestamp":now,"event_id":eid,"action":"SETTLE","shares":b["shares"],"cost":b["cost"],"payout":payout,"net_edge":b["net_edge"],"pnl":pnl}); del open_b[eid]
        except Exception as exc:
            if len(failures)<20:failures.append(f"settle:{eid}:{type(exc).__name__}")

    locked_value=sum(float(b["shares"]) for b in open_b.values()); locked_cost=sum(float(b["cost"]) for b in open_b.values()); equity=cash+locked_value; peak=max(peak,equity); dd=max(0.0,1-equity/peak) if peak else 0.0; killed=bool(state.get("killed")) or dd>=max_dd

    if not killed:
        try:eids=discover_event_ids(gamma,args.markets,args.min_liquidity)[:args.max_events]
        except Exception as exc:eids=[]; failures.append(f"discover:{type(exc).__name__}:{exc}")
        slip=max(0.0,args.slippage_bps)/10000.0
        for eid in eids:
            if eid in open_b:continue
            try:
                spec=event_spec(gamma,eid)
                if spec is None:continue
                ms,_=spec; tokens=[]; rates=[]
                for m in ms:
                    yes,_=market_tokens(m) or ("",""); tokens.append(yes)
                    fs=m.get("feeSchedule") if isinstance(m.get("feeSchedule"),dict) else {}; rates.append((max(0.0,finite(fs.get("rate"),.07)),max(0.0,finite(fs.get("exponent"),1.0))))
                bs=books(clob,tokens)
                if any(t not in bs for t in tokens):continue
                scanned+=1; raw=sum(bs[t]["ask"] for t in tokens); px=[min(.999999,bs[t]["ask"]*(1+slip)) for t in tokens]; cost_ps=sum(p+fee_ps(p,*rates[i]) for i,p in enumerate(px)); edge=1.0-cost_ps; best_edge=max(best_edge,edge); candidates+=int(edge>0)
                if edge<=args.min_edge:continue
                min_size=min(bs[t]["size"] for t in tokens); min_order=max(bs[t]["min_order"] for t in tokens); eq=max(1.0,equity); room=min(args.max_trade_usd,max(0.0,max_gross*eq-locked_cost),max(0.0,max_event*eq),cash)
                shares=min(min_size,room/max(cost_ps,1e-9))
                if shares+1e-12<min_order:continue
                cost=shares*cost_ps
                if cost>cash+1e-9:continue
                # All-or-none paper admission: every leg has displayed touch depth for
                # the identical share count in the same fetched snapshot.
                cash-=cost; open_b[eid]={"shares":shares,"cost":cost,"net_edge":edge,"raw_edge":1-raw,"opened_ts":now,"legs":len(ms)}; locked_cost+=cost; equity=cash+sum(float(x["shares"]) for x in open_b.values()); entered+=1
                append_csv(args.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","pnl"],{"timestamp":now,"event_id":eid,"action":"BUY_COMPLETE_YES_SET","shares":shares,"cost":cost,"payout":shares,"net_edge":edge,"pnl":0.0})
            except Exception as exc:
                if len(failures)<20:failures.append(f"event:{eid}:{type(exc).__name__}")
    peak=max(peak,equity); dd=max(0.0,1-equity/peak) if peak else 0.0; killed=killed or dd>=max_dd
    state={"timestamp":now,"cash":cash,"equity":equity,"peak":peak,"drawdown":dd,"killed":killed,"bundles":open_b,"gross_exposure":locked_cost,"open_positions":len(open_b),"realized_pnl":realized,"scanned_events":scanned,"positive_candidates":candidates,"entered":entered,"best_edge":best_edge,"failures":failures,"paper_only":True,"atomic_snapshot_assumption":True}; atomic_json(sp,state); atomic_json(args.run_dir/"status.json",state)
    append_csv(args.run_dir/"equity.csv",["timestamp","cash","equity","drawdown","gross_exposure","open_positions","scanned_events","positive_candidates","entered","best_edge"],state)
    print(json.dumps({k:state[k] for k in ("equity","drawdown","open_positions","scanned_events","positive_candidates","entered","best_edge","killed")},sort_keys=True)); return 0


if __name__=="__main__": raise SystemExit(main())
