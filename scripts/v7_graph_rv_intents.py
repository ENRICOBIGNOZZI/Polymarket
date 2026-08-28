#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_graph_rv import Book, parse_book
from v7_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details

FIELDS = ["bundle_id","strategy","event_id","created_ts","mode","expected_edge","max_notional","market_id","side","weight","limit_price","execution_deadline_ts","hold_deadline_ts"]


def parse_ts(value: Any) -> int:
    if isinstance(value, (int,float)):
        raw=int(value); return raw//1000 if raw>10_000_000_000 else raw
    text=str(value or "").strip()
    if not text: return 0
    try:
        raw=int(float(text)); return raw//1000 if raw>10_000_000_000 else raw
    except ValueError: pass
    try:
        parsed=dt.datetime.fromisoformat(text.replace("Z","+00:00"));
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except ValueError: return 0

@dataclass(frozen=True)
class Market:
    market_id:str; condition_id:str; event_id:str; yes_token:str; liquidity:float; neg_risk:bool; end_ts:int; raw:dict[str,Any]


def parse_market(raw: dict[str,Any]) -> Market | None:
    ids=[str(x) for x in parse_array(raw.get("clobTokenIds"))]; outcomes=[str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    if len(ids)<2: return None
    yes=next((i for i,name in enumerate(outcomes[:len(ids)]) if name=="YES"),0)
    market_id=str(raw.get("id") or ""); condition=str(raw.get("conditionId") or "")
    if not market_id or not condition: return None
    event=str(raw.get("eventId") or "")
    events=raw.get("events")
    if not event and isinstance(events,list) and events and isinstance(events[0],dict): event=str(events[0].get("id") or "")
    return Market(market_id,condition,event or condition,ids[yes],max(0.0,finite(raw.get("liquidityNum"),finite(raw.get("liquidity"),0.0))),bool(raw.get("negRisk",False)),parse_ts(raw.get("endDate") or raw.get("endDateIso")),raw)


def discover(gamma:str, limit:int, min_liquidity:float) -> list[Market]:
    out=[]; offset=0
    for _ in range(200):
        if limit>0 and len(out)>=limit: break
        page=500 if limit<=0 else min(500,limit-len(out))
        q=urllib.parse.urlencode({"active":"true","closed":"false","limit":page,"offset":offset,"order":"liquidityNum","ascending":"false"})
        root=request_json(f"{gamma}/markets?{q}"); batch=root if isinstance(root,list) else root.get("markets",[]) if isinstance(root,dict) else []
        if not batch: break
        for raw in batch:
            market=parse_market(raw) if isinstance(raw,dict) else None
            if market and market.liquidity>=min_liquidity: out.append(market)
            if limit>0 and len(out)>=limit: break
        if len(batch)<page: break
        offset+=len(batch)
    else:
        raise RuntimeError("Gamma graph discovery pagination guard reached before exhaustion")
    return out


def structural_scan_budget(cfg:dict[str,Any]) -> int:
    v7=cfg.get("v7") or {}
    path=Path(str(v7.get("adaptive_universe_policy") or "config/v7_adaptive_universe.json"))
    if not path.is_absolute(): path=Path(__file__).resolve().parents[1]/path
    policy=json.loads(path.read_text(encoding="utf-8"))
    structural=((policy.get("resource_budget") or {}).get("structural") or {})
    budget=float(structural.get("scan_time_budget_millis",0)); cost=float(structural.get("estimated_event_scan_millis",0))
    if budget<=0 or cost<=0: raise ValueError("invalid adaptive structural scan budget")
    return max(1,int(budget//cost))


def rotating_events(event_ids:list[str],now:int,budget:int,cycle_seconds:int) -> tuple[list[str],int]:
    if not event_ids: return [],0
    cycle=max(1,now//max(1,cycle_seconds)); start=(cycle*budget)%len(event_ids)
    count=min(len(event_ids),budget)
    return [event_ids[(start+i)%len(event_ids)] for i in range(count)],start


def books(clob:str,tokens:list[str]) -> dict[str,Book]:
    out={}; unique=list(dict.fromkeys(t for t in tokens if t))
    for start in range(0,len(unique),80):
        root=request_json(f"{clob}/books",[{"token_id":t} for t in unique[start:start+80]]); received=time.time_ns()//1_000_000
        for raw in root if isinstance(root,list) else []:
            if isinstance(raw,dict):
                book=parse_book(raw,received)
                if book: out[book.token]=book
    return out


def atomic_csv(path:Path,rows:list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    with tmp.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=FIELDS); writer.writeheader(); writer.writerows(rows)
    os.replace(tmp,path)


def scan(cfg:dict[str,Any], now:int) -> tuple[list[dict[str,Any]],dict[str,int]]:
    gamma=str(cfg["gamma_url"]).rstrip("/"); clob=str(cfg["clob_url"]).rstrip("/")
    markets=discover(gamma,0,float(cfg.get("min_liquidity",2.0)))
    initial=books(clob,[m.yes_token for m in markets]); event_ids=list(dict.fromkeys(m.event_id for m in markets if m.neg_risk and m.event_id))
    rows=[]; stats={"events_considered":0,"events_complete":0,"bundles":0}; v7=cfg.get("v7") or {}
    budget=structural_scan_budget(cfg); selected,cursor=rotating_events(event_ids,now,budget,int(v7.get("graph_scan_seconds",15)))
    stats.update({"discovered_markets":len(markets),"discovered_events":len(event_ids),"scan_budget_events":budget,"scan_cursor":cursor,"discovery_exhaustive":True})
    min_edge=float(cfg.get("min_net_edge",.00005)); allocation=float(v7.get("relative_value_capital_fraction",.34)); capital=float(cfg.get("starting_capital",10000.0)); max_notional=max(0.0,capital*allocation)
    for event_id in selected:
        stats["events_considered"]+=1
        try: event=request_json(f"{gamma}/events/{event_id}")
        except Exception: continue
        if not isinstance(event,dict) or not event.get("negRisk") or event.get("negRiskAugmented"): continue
        raw_markets=event.get("markets")
        if not isinstance(raw_markets,list) or len(raw_markets)<2: continue
        parsed=[parse_market(raw) for raw in raw_markets if isinstance(raw,dict)]
        if len(parsed)!=len(raw_markets) or any(m is None for m in parsed): continue
        em=[m for m in parsed if m is not None]
        missing=[m.yes_token for m in em if m.yes_token not in initial]
        if missing:
            try: initial.update(books(clob,missing))
            except Exception: continue
        if any(m.yes_token not in initial for m in em): continue
        cost=0.0; valid=True
        for m in em:
            book=initial[m.yes_token]; fee=resolve_fee_details(m.raw,clob,m.condition_id,m.yes_token)
            if not fee.verified: valid=False; break
            cost+=book.bid+fee_per_share(book.bid,fee,taker=False)
        edge=1.0-cost
        if not valid or edge<=min_edge or max_notional<=0: continue
        # max_notional is a risk allocation only. Queue and same-side maker depth never grant capacity here.
        end=max((m.end_ts for m in em),default=0); execution=now+int(v7.get("graph_execution_timeout_seconds",300)); hold=max(now+int(v7.get("graph_hold_seconds",3600)),end+3600 if end else now+3600)
        bucket=now//3600; bundle=f"GRAPH_RV:{event_id}:{bucket}"
        for m in em:
            book=initial[m.yes_token]
            rows.append({"bundle_id":bundle,"strategy":"GRAPH_RV","event_id":event_id,"created_ts":now,"mode":"POLICY","expected_edge":edge,"max_notional":max_notional,"market_id":m.market_id,"side":"YES","weight":1.0,"limit_price":book.bid,"execution_deadline_ts":execution,"hold_deadline_ts":hold})
        stats["events_complete"]+=1; stats["bundles"]+=1
    return rows,stats


def main()->int:
    p=argparse.ArgumentParser(description="V7 Graph/RV structural intent scanner")
    p.add_argument("--config",type=Path,default=Path("config/paper_v7.json")); p.add_argument("--output",type=Path,required=True); p.add_argument("--status",type=Path,required=True)
    a=p.parse_args(); cfg=json.loads(a.config.read_text())
    if cfg.get("paper_only") is not True or (cfg.get("v7") or {}).get("authenticated_execution") is not False: raise SystemExit("PAPER-only V7 config required")
    now=int(time.time()); failures=[]
    try: rows,stats=scan(cfg,now)
    except Exception as exc: rows=[]; stats={"events_considered":0,"events_complete":0,"bundles":0}; failures=[f"{type(exc).__name__}:{exc}"]
    rows.sort(key=lambda row:(float(row["expected_edge"]),row["bundle_id"]),reverse=True); atomic_csv(a.output,rows)
    status={"schema":"polymarket_v7_graph_rv_scan_status_v2","timestamp":now,"paper_only":True,"intent_rows":len(rows),"bundles":len({r['bundle_id'] for r in rows}),"graph_rv":stats,"queue_grants_capital":False,"downstream_unwind_depth_sizing_required":True,"downstream_direct_joint_state_required":True,"failures":failures}
    atomic= a.status.with_name(a.status.name+f".tmp.{os.getpid()}"); a.status.parent.mkdir(parents=True,exist_ok=True); atomic.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n"); os.replace(atomic,a.status); print(json.dumps(status,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
