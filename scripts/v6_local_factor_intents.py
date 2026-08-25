#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
THRESHOLD = re.compile(r"([$€£]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|b|%|bp|bps)?)", re.I)
DIRECTION = re.compile(r"\b(above|below|over|under|reach|exceed|dip|at least|at most|more than|less than)\b", re.I)


def finite(value: Any, default=math.nan) -> float:
    try: x = float(value)
    except (TypeError, ValueError, OverflowError): return default
    return x if math.isfinite(x) else default


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list): return value
    if isinstance(value, str):
        try:
            x = json.loads(value)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError: return []
    return []


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent":"polymarket-v6-paper/1","Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


@dataclass
class Market:
    market_id: str; event_id: str; question: str; yes: str; no: str; liquidity: float


@dataclass
class Book:
    bid: float; ask: float; bid_size: float; min_order: float


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in parse_array(raw.get("outcomes"))]
    if len(ids) < 2: return None
    yi, ni = 0, 1
    for i, x in enumerate(outcomes[:len(ids)]):
        if x == "yes": yi = i
        elif x == "no": ni = i
    mid = str(raw.get("id") or ""); q = str(raw.get("question") or "")
    if not mid or not q: return None
    event = str(raw.get("eventId") or "")
    events = raw.get("events")
    if not event and isinstance(events, list) and events and isinstance(events[0], dict): event = str(events[0].get("id") or "")
    return Market(mid, event or str(raw.get("conditionId") or mid), q, ids[yi], ids[ni], max(0.0, finite(raw.get("liquidityNum"), 0.0)))


def discover(gamma: str, limit: int, min_liq: float) -> list[Market]:
    out=[]; offset=0
    while len(out)<limit and offset<5000:
        params=urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        raw=request_json(f"{gamma}/markets?{params}")
        batch=raw if isinstance(raw,list) else raw.get("markets",[]) if isinstance(raw,dict) else []
        if not batch: break
        for row in batch:
            m=parse_market(row) if isinstance(row,dict) else None
            if m and m.liquidity>=min_liq: out.append(m)
            if len(out)>=limit: break
        if len(batch)<100: break
        offset+=100
    return out


def payoff_family(question: str) -> str | None:
    if not THRESHOLD.search(question) or not DIRECTION.search(question): return None
    x=question.lower(); x=THRESHOLD.sub(" <threshold> ",x); x=DIRECTION.sub(" <direction> ",x)
    x=re.sub(r"\b20\d{2}\b"," <year> ",x); x=re.sub(r"[^a-z<>]+"," ",x)
    return re.sub(r"\s+"," ",x).strip()


def clusters(markets: list[Market], max_clusters: int) -> list[tuple[str,list[Market]]]:
    groups: dict[str,list[Market]]=defaultdict(list)
    for m in markets:
        groups["event:"+m.event_id].append(m)
        fam=payoff_family(m.question)
        if fam: groups["payoff:"+fam].append(m)
    candidates=[(k,v) for k,v in groups.items() if 3<=len(v)<=25]
    candidates.sort(key=lambda kv: sum(x.liquidity for x in kv[1]), reverse=True)
    seen=set(); out=[]
    for key, ms in candidates:
        ids=tuple(sorted(m.market_id for m in ms))
        if ids in seen: continue
        seen.add(ids); out.append((key,ms))
        if len(out)>=max_clusters: break
    return out


def fetch_books(clob: str, markets: list[Market]) -> dict[str,Book]:
    tokens=[t for m in markets for t in (m.yes,m.no)]; out={}
    for i in range(0,len(tokens),80):
        raw=request_json(clob+"/books",[{"token_id":x} for x in tokens[i:i+80]])
        for row in raw if isinstance(raw,list) else []:
            if not isinstance(row,dict): continue
            token=str(row.get("asset_id") or ""); bids=[]; asks=[]
            for z in row.get("bids",[]):
                if isinstance(z,dict):
                    p,q=finite(z.get("price")),finite(z.get("size"),0)
                    if math.isfinite(p) and 0<p<1 and q>0: bids.append((p,q))
            for z in row.get("asks",[]):
                if isinstance(z,dict):
                    p,q=finite(z.get("price")),finite(z.get("size"),0)
                    if math.isfinite(p) and 0<p<1 and q>0: asks.append((p,q))
            if token and bids and asks:
                bids.sort(reverse=True); asks.sort()
                out[token]=Book(bids[0][0],asks[0][0],bids[0][1],max(1.0,finite(row.get("min_order_size"),1.0)))
    return out


def history(clob: str, token: str, start: int, end: int, fidelity: int) -> dict[int,float]:
    url=f"{clob}/prices-history?market={urllib.parse.quote(token)}&startTs={start}&endTs={end}&fidelity={fidelity}"
    raw=request_json(url)
    rows=raw.get("history",[]) if isinstance(raw,dict) else []
    bucket=fidelity*60; out={}
    for z in rows:
        if not isinstance(z,dict): continue
        t=int(finite(z.get("t"),0)); p=finite(z.get("p"))
        if t>0 and math.isfinite(p) and 0<p<1: out[(t//bucket)*bucket]=math.log(p/(1-p))
    return out


def ar_fit(resid: list[float]) -> tuple[float,float,float,float]:
    if len(resid)<20: return 1.0,0.0,0.0,0.0
    mu=statistics.fmean(resid); sd=statistics.stdev(resid)
    if sd<1e-6: return 1.0,0.0,mu,sd
    lag=resid[:-1]; dr=[resid[i]-resid[i-1] for i in range(1,len(resid))]
    ml,md=statistics.fmean(lag),statistics.fmean(dr)
    sxx=sum((x-ml)**2 for x in lag); sxy=sum((x-ml)*(y-md) for x,y in zip(lag,dr))
    if sxx<1e-10: return 1.0,0.0,mu,sd
    gamma=sxy/sxx; c=md-gamma*ml
    rss=sum((y-(c+gamma*x))**2 for x,y in zip(lag,dr)); sigma2=rss/max(1,len(lag)-2)
    se=math.sqrt(max(0.0,sigma2)/sxx); t=gamma/se if se>1e-12 else 0.0
    return 1+gamma,t,mu,sd


def build_cluster_intent(key: str, ms: list[Market], books: dict[str,Book], series: dict[str,dict[int,float]], now: int, min_z: float, min_t: float, min_edge: float, max_trade: float, serial: int) -> list[dict[str,Any]]:
    usable=[m for m in ms if m.market_id in series and len(series[m.market_id])>=24 and m.yes in books and m.no in books]
    if len(usable)<3: return []
    common=set(series[usable[0].market_id])
    for m in usable[1:]: common &= set(series[m.market_id])
    times=sorted(common)
    if len(times)<24: return []
    vals={m.market_id:[series[m.market_id][t] for t in times] for m in usable}
    factor=[statistics.fmean(vals[m.market_id][j] for m in usable) for j in range(len(times))]
    signals=[]
    for m in usable:
        r=[x-f for x,f in zip(vals[m.market_id],factor)]
        phi,tstat,mu,sd=ar_fit(r)
        if not (0.02<phi<0.999 and tstat<=-min_t and sd>0): continue
        z=(r[-1]-mu)/sd
        if abs(z)<min_z: continue
        expected_logit=(phi-1.0)*(r[-1]-mu)
        signals.append((abs(z)*abs(tstat),z,expected_logit,m,r[-1]))
    if len(signals)<2: return []
    signals.sort(reverse=True,key=lambda x:x[0])
    # Pair the strongest dislocation with the strongest opposite-signed residual.
    a=signals[0]; opp=next((x for x in signals[1:] if x[1]*a[1]<0),None)
    if opp is None: return []
    legs=[]; total_expected=0.0; capital=0.0; capacity=math.inf; minimum=1.0
    for sig in (a,opp):
        _,z,move,m,_=sig
        side="NO" if z>0 else "YES"; b=books[m.no if side=="NO" else m.yes]
        p=max(0.001,min(0.999,b.bid)); dp=abs(move)*max(0.01,p*(1-p))
        total_expected+=dp; capital+=p; capacity=min(capacity,b.bid_size); minimum=max(minimum,b.min_order)
        legs.append((m,side,b))
    edge=total_expected/max(capital,1e-6)
    if edge<=min_edge or capacity+1e-12<minimum: return []
    max_notional=min(max_trade,capacity*capital)
    bundle=f"LOCAL_FACTOR-{now}-{serial}"; hold=now+6*3600; deadline=now+180
    return [{"bundle_id":bundle,"strategy":"LOCAL_FACTOR","event_id":key,"created_ts":now,"mode":"MAKER","expected_edge":edge,"max_notional":max_notional,"market_id":m.market_id,"side":side,"weight":1.0,"limit_price":b.bid,"execution_deadline_ts":deadline,"hold_deadline_ts":hold} for m,side,b in legs]


def atomic_csv(path:Path,rows:list[dict[str,Any]])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    os.replace(tmp,path)


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--markets",type=int,default=500); ap.add_argument("--min-liquidity",type=float,default=10); ap.add_argument("--lookback-hours",type=int,default=336); ap.add_argument("--fidelity-minutes",type=int,default=60); ap.add_argument("--max-clusters",type=int,default=30); ap.add_argument("--min-z",type=float,default=.65); ap.add_argument("--min-t",type=float,default=.75); ap.add_argument("--min-edge",type=float,default=.0002); ap.add_argument("--max-trade-usd",type=float,default=60); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; now=int(time.time()); failures=[]; rows=[]; histories=0; serial=0
    try: ms=discover(gamma,args.markets,args.min_liquidity); cs=clusters(ms,args.max_clusters); selected={m.market_id:m for _,group in cs for m in group}; books=fetch_books(clob,list(selected.values()))
    except Exception as exc: ms=[]; cs=[]; selected={}; books={}; failures.append(f"market_data:{type(exc).__name__}:{exc}")
    start=now-args.lookback_hours*3600; series={}
    for m in selected.values():
        try:
            h=history(clob,m.yes,start,now,args.fidelity_minutes)
            if len(h)>=24: series[m.market_id]=h; histories+=1
        except Exception as exc:
            if len(failures)<20: failures.append(f"history:{m.market_id}:{type(exc).__name__}")
    for key,group in cs:
        intent=build_cluster_intent(key,group,books,series,now,args.min_z,args.min_t,args.min_edge,args.max_trade_usd,serial)
        if intent: rows.extend(intent); serial+=1
    atomic_csv(args.output,rows)
    status={"timestamp":now,"paper_only":True,"markets":len(ms),"clusters":len(cs),"histories":histories,"bundles":serial,"intent_rows":len(rows),"best_edge":max((float(r["expected_edge"]) for r in rows),default=0.0),"failures":failures}
    args.status.parent.mkdir(parents=True,exist_ok=True); tmp=args.status.with_suffix(args.status.suffix+".tmp"); tmp.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n"); os.replace(tmp,args.status); print(json.dumps(status,sort_keys=True)); return 0


if __name__=="__main__": raise SystemExit(main())
