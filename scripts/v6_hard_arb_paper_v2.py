#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from v6_market_common import FeeDetails, fee_per_share, finite, parse_array, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts.v6_market_common import FeeDetails, fee_per_share, finite, parse_array, request_json, resolve_fee_details


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); exists=path.exists() and path.stat().st_size>0
    with path.open("a",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        if not exists: writer.writeheader()
        writer.writerow({key:row.get(key,"") for key in fields})


def market_tokens(raw: dict[str, Any]) -> tuple[str,str] | None:
    ids=[str(x) for x in parse_array(raw.get("clobTokenIds"))]; outcomes=[str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
    if len(ids)<2: return None
    yi,ni=0,1
    for i,name in enumerate(outcomes[:len(ids)]):
        if name=="yes": yi=i
        elif name=="no": ni=i
    return ids[yi],ids[ni]


def discover_event_ids(gamma:str,market_limit:int,min_liquidity:float,max_events:int)->list[str]:
    output=[]; remaining=max(0,int(market_limit)); offset=0
    while remaining>0 and offset<5000 and len(output)<max_events:
        page_size=min(100,remaining); query=urllib.parse.urlencode({"active":"true","closed":"false","limit":page_size,"offset":offset,"order":"liquidityNum","ascending":"false"}); root=request_json(gamma.rstrip("/")+"/markets?"+query); batch=root if isinstance(root,list) else root.get("markets",[]) if isinstance(root,dict) else []
        if not batch: break
        for raw in batch:
            if not isinstance(raw,dict) or not raw.get("negRisk"): continue
            if finite(raw.get("liquidityNum"),finite(raw.get("liquidity"),0.0))<min_liquidity: continue
            event_id=str(raw.get("eventId") or ""); events=raw.get("events")
            if not event_id and isinstance(events,list) and events and isinstance(events[0],dict): event_id=str(events[0].get("id") or "")
            if event_id and event_id not in output: output.append(event_id)
            if len(output)>=max_events: break
        consumed=len(batch); remaining-=consumed; offset+=consumed
        if consumed<page_size: break
    return output


def event_spec(gamma:str,event_id:str)->list[dict[str,Any]]|None:
    event=request_json(f"{gamma.rstrip('/')}/events/{event_id}")
    if not isinstance(event,dict) or not event.get("negRisk") or event.get("negRiskAugmented"): return None
    markets=event.get("markets")
    if not isinstance(markets,list) or len(markets)<2: return None
    clean=[]
    for raw in markets:
        if not isinstance(raw,dict) or market_tokens(raw) is None: return None
        if raw.get("closed") or raw.get("active") is False or raw.get("enableOrderBook") is False or raw.get("acceptingOrders") is False: return None
        clean.append(raw)
    return clean


@dataclass
class Book:
    token:str
    asks:list[tuple[float,float]]
    min_order:float
    @property
    def total_depth(self)->float: return sum(size for _,size in self.asks)


def fetch_books(clob:str,tokens:list[str])->dict[str,Book]:
    output={}
    for i in range(0,len(tokens),80):
        root=request_json(clob.rstrip("/")+"/books",[{"token_id":x} for x in tokens[i:i+80]])
        for raw in root if isinstance(root,list) else []:
            if not isinstance(raw,dict): continue
            token=str(raw.get("asset_id") or ""); asks=[]
            for row in raw.get("asks",[]):
                if not isinstance(row,dict): continue
                price,size=finite(row.get("price")),finite(row.get("size"),0.0)
                if math.isfinite(price) and 0<price<1 and size>0: asks.append((price,size))
            asks.sort()
            if token and asks: output[token]=Book(token,asks,max(1.0,finite(raw.get("min_order_size"),1.0)))
    return output


def taker_buy_cost(book:Book,shares:float,slippage_bps:float,fee:FeeDetails)->tuple[float,float,float]|None:
    if shares<=0: return 0.0,0.0,0.0
    remaining=shares; raw_cash=fee_cash=0.0; slip=max(0.0,slippage_bps)/10000.0
    for price,depth in book.asks:
        size=min(remaining,depth)
        if size<=0: continue
        executed=min(0.999999,price*(1.0+slip)); raw_cash+=size*executed; fee_cash+=size*fee_per_share(executed,fee,taker=True); remaining-=size
        if remaining<=1e-9: break
    if remaining>1e-8: return None
    return raw_cash+fee_cash,raw_cash/shares,fee_cash


def max_executable_shares(books:list[Book],fees:list[FeeDetails],*,cash_room:float,max_trade_usd:float,min_edge:float,slippage_bps:float)->tuple[float,float,float,float]|None:
    min_order=max(book.min_order for book in books); max_depth=min(book.total_depth for book in books)
    if max_depth+1e-12<min_order: return None
    room=min(max(0.0,cash_room),max(0.0,max_trade_usd))
    if room<=0: return None
    def economics(shares:float):
        total=fees_paid=0.0
        for book,fee in zip(books,fees):
            item=taker_buy_cost(book,shares,slippage_bps,fee)
            if item is None: return None
            total+=item[0]; fees_paid+=item[2]
        return total,1.0-total/max(shares,1e-12),fees_paid
    low=min_order; low_econ=economics(low)
    if low_econ is None or low_econ[0]>room+1e-9 or low_econ[1]<=min_edge: return None
    high=max_depth; high_econ=economics(high)
    if high_econ is not None and high_econ[0]<=room+1e-9 and high_econ[1]>min_edge: return high,high_econ[1],high_econ[0],high_econ[2]
    best=(low,low_econ[1],low_econ[0],low_econ[2])
    for _ in range(36):
        mid=0.5*(low+high); item=economics(mid); feasible=item is not None and item[0]<=room+1e-9 and item[1]>min_edge
        if feasible: best=(mid,item[1],item[0],item[2]); low=mid
        else: high=mid
    return best


def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,required=True); parser.add_argument("--run-dir",type=Path,required=True); parser.add_argument("--markets",type=int,default=700); parser.add_argument("--min-liquidity",type=float,default=10); parser.add_argument("--max-events",type=int,default=80); parser.add_argument("--min-edge",type=float,default=0.0002); parser.add_argument("--max-trade-usd",type=float,default=60); parser.add_argument("--slippage-bps",type=float,default=5); parser.add_argument("--allow-unverified-fee",action="store_true"); args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding="utf-8")); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; starting=float(cfg["starting_capital"]); max_drawdown=float(cfg.get("max_drawdown",0.15)); max_gross=float(cfg.get("max_gross_fraction",0.45)); max_event=float(cfg.get("max_event_fraction",0.08)); now=int(time.time()); args.run_dir.mkdir(parents=True,exist_ok=True); state_path=args.run_dir/"state.json"
    state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cash":starting,"peak":starting,"killed":False,"bundles":{},"realized_pnl":0.0}; cash=finite(state.get("cash"),starting); peak=max(starting,finite(state.get("peak"),starting)); open_bundles=state.get("bundles") if isinstance(state.get("bundles"),dict) else {}; realized=finite(state.get("realized_pnl"),0.0); failures=[]; scanned=candidates=entered=0; best_edge=0.0; fee_unverified_events=0
    for event_id,bundle in list(open_bundles.items()):
        try:
            event=request_json(f"{gamma.rstrip('/')}/events/{event_id}")
            if isinstance(event,dict) and event.get("closed"):
                payout=float(bundle["shares"]); cash+=payout; pnl=payout-float(bundle["cost"]); realized+=pnl; append_csv(args.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","fees","pnl"],{"timestamp":now,"event_id":event_id,"action":"SETTLE","shares":bundle["shares"],"cost":bundle["cost"],"payout":payout,"net_edge":bundle["net_edge"],"fees":bundle.get("fees",0.0),"pnl":pnl}); del open_bundles[event_id]
        except Exception as exc:
            if len(failures)<20: failures.append(f"settle:{event_id}:{type(exc).__name__}")
    locked_cost=sum(float(bundle["cost"]) for bundle in open_bundles.values()); locked_profit=sum(float(bundle["shares"])-float(bundle["cost"]) for bundle in open_bundles.values()); equity=cash+locked_cost; peak=max(peak,equity); drawdown=max(0.0,1.0-equity/peak) if peak else 0.0; killed=bool(state.get("killed")) or drawdown>=max_drawdown
    if not killed:
        try: event_ids=discover_event_ids(gamma,args.markets,args.min_liquidity,args.max_events)
        except Exception as exc: event_ids=[]; failures.append(f"discover:{type(exc).__name__}:{exc}")
        for event_id in event_ids:
            if event_id in open_bundles: continue
            try:
                markets=event_spec(gamma,event_id)
                if markets is None: continue
                tokens=[(market_tokens(raw) or ("",""))[0] for raw in markets]; live_books=fetch_books(clob,tokens)
                if any(token not in live_books for token in tokens): continue
                scanned+=1; fees=[]; fee_sources=[]; verified=True
                for raw,token in zip(markets,tokens):
                    details=resolve_fee_details(raw,clob,str(raw.get("conditionId") or ""),token); fees.append(details); fee_sources.append(details.source); verified=verified and details.verified
                if not verified and not args.allow_unverified_fee: fee_unverified_events+=1; continue
                eq=max(1.0,equity); room=min(args.max_trade_usd,max(0.0,max_gross*eq-locked_cost),max(0.0,max_event*eq),cash); executable=max_executable_shares([live_books[token] for token in tokens],fees,cash_room=room,max_trade_usd=args.max_trade_usd,min_edge=args.min_edge,slippage_bps=args.slippage_bps)
                if executable is None:
                    touch_cost=0.0; possible=True
                    for token,fee in zip(tokens,fees):
                        book=live_books[token]; item=taker_buy_cost(book,book.min_order,args.slippage_bps,fee)
                        if item is None: possible=False; break
                        touch_cost+=item[0]/book.min_order
                    if possible: best_edge=max(best_edge,1.0-touch_cost); candidates+=int(1.0-touch_cost>0.0)
                    continue
                shares,edge,cost,fees_paid=executable; best_edge=max(best_edge,edge); candidates+=int(edge>0.0)
                if edge<=args.min_edge or cost>cash+1e-9: continue
                cash-=cost; open_bundles[event_id]={"shares":shares,"cost":cost,"net_edge":edge,"opened_ts":now,"legs":len(markets),"fees":fees_paid,"fee_sources":fee_sources,"execution":"multi_level_vwap"}; locked_cost+=cost; locked_profit+=shares-cost; equity=cash+locked_cost; entered+=1; append_csv(args.run_dir/"fills.csv",["timestamp","event_id","action","shares","cost","payout","net_edge","fees","pnl"],{"timestamp":now,"event_id":event_id,"action":"BUY_COMPLETE_YES_SET_VWAP","shares":shares,"cost":cost,"payout":shares,"net_edge":edge,"fees":fees_paid,"pnl":0.0})
            except Exception as exc:
                if len(failures)<20: failures.append(f"event:{event_id}:{type(exc).__name__}:{exc}")
    peak=max(peak,equity); drawdown=max(0.0,1.0-equity/peak) if peak else 0.0; killed=killed or drawdown>=max_drawdown; state={"timestamp":now,"cash":cash,"equity":equity,"peak":peak,"drawdown":drawdown,"killed":killed,"bundles":open_bundles,"gross_exposure":locked_cost,"open_positions":len(open_bundles),"realized_pnl":realized,"locked_expected_profit":locked_profit,"scanned_events":scanned,"positive_candidates":candidates,"entered":entered,"best_edge":best_edge,"fee_unverified_events":fee_unverified_events,"failures":failures,"paper_only":True,"atomic_snapshot_assumption":True,"book_costing":"multi_level_vwap","marking":"cost_basis_until_resolution"}; atomic_json(state_path,state); atomic_json(args.run_dir/"status.json",state); append_csv(args.run_dir/"equity.csv",["timestamp","cash","equity","drawdown","gross_exposure","open_positions","realized_pnl","locked_expected_profit","best_edge","entered","killed"],state); print(json.dumps({"scanned_events":scanned,"positive_candidates":candidates,"entered":entered,"best_edge":best_edge,"fee_unverified_events":fee_unverified_events,"realized_pnl":realized,"killed":killed},sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
