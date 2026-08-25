#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from v6_market_common import FeeDetails, TapeFlow, fee_per_share, finite, parse_array, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts.v6_market_common import FeeDetails, TapeFlow, fee_per_share, finite, parse_array, request_json, resolve_fee_details
try:
    from v6_micro_target import label_matured_samples
except ModuleNotFoundError:
    from scripts.v6_micro_target import label_matured_samples

FEATURE_VERSION = 2
FEATURE_COUNT = 10


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


class Market:
    def __init__(self, raw: dict[str, Any]):
        ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
        outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
        if len(ids) < 2:
            raise ValueError("missing tokens")
        yi, ni = 0, 1
        for i, name in enumerate(outcomes[: len(ids)]):
            if name == "yes": yi = i
            elif name == "no": ni = i
        self.raw = raw
        self.id = str(raw.get("id") or "")
        self.condition = str(raw.get("conditionId") or "")
        self.event = str(raw.get("eventId") or self.condition or self.id)
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            self.event = str(events[0].get("id") or self.event)
        self.slug = str(raw.get("slug") or self.id)
        self.category = str(raw.get("category") or "unknown").strip().lower() or "unknown"
        self.yes = ids[yi]
        self.no = ids[ni]
        self.liq = max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0)))


class Book:
    def __init__(self, raw: dict[str, Any]):
        self.token = str(raw.get("asset_id") or "")
        self.tick = max(1e-6, finite(raw.get("tick_size"), 0.01))
        self.min_order = max(1.0, finite(raw.get("min_order_size"), 1.0))
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        for key, output in (("bids", self.bids), ("asks", self.asks)):
            for row in raw.get(key, []):
                if not isinstance(row, dict): continue
                price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                if math.isfinite(price) and 0 < price < 1 and size > 0: output.append((price, size))
        self.bids.sort(reverse=True); self.asks.sort()

    def bid(self) -> float: return self.bids[0][0] if self.bids else math.nan
    def ask(self) -> float: return self.asks[0][0] if self.asks else math.nan
    def mid(self) -> float:
        bid, ask = self.bid(), self.ask(); return 0.5 * (bid + ask) if math.isfinite(bid) and math.isfinite(ask) else math.nan
    def spread(self) -> float:
        bid, ask = self.bid(), self.ask(); return ask - bid if math.isfinite(bid) and math.isfinite(ask) else math.nan
    def depth(self, bid_side: bool, n: int = 5) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels: return 0.0
        best = levels[0][0]; scale = max(1e-4, 3 * self.tick)
        return sum(size * math.exp(-abs(price - best) / scale) for price, size in levels[:n])
    def micro(self) -> float:
        bid, ask = self.bid(), self.ask(); db, da = self.depth(True), self.depth(False)
        if not math.isfinite(bid) or not math.isfinite(ask): return math.nan
        return (ask * db + bid * da) / (db + da) if db + da > 1e-12 else 0.5 * (bid + ask)


def discover(gamma: str, limit: int, min_liq: float) -> list[Market]:
    output: list[Market] = []; offset = 0
    while len(output) < limit and offset < 5000:
        query = urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        root = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not batch: break
        for raw in batch:
            if not isinstance(raw, dict): continue
            try: market = Market(raw)
            except ValueError: continue
            if market.id and market.condition and market.liq >= min_liq: output.append(market)
            if len(output) >= limit: break
        if len(batch) < 100: break
        offset += 100
    return output


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [token for market in markets for token in (market.yes, market.no)]; output: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        root = request_json(clob.rstrip("/") + "/books", [{"token_id": x} for x in tokens[i : i + 80]])
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict): continue
            book = Book(raw)
            if book.token and book.bids and book.asks: output[book.token] = book
    return output


def features(y: Book, n: Book, flow: TapeFlow, flow_window: int) -> tuple[list[float], float, float] | None:
    mid = y.mid(); spread = max(y.spread(), n.spread())
    if not math.isfinite(mid) or not math.isfinite(spread) or spread <= 0: return None
    ym, nm = y.micro(), n.micro(); dyb, dya = y.depth(True), y.depth(False); dnb, dna = n.depth(True), n.depth(False)
    if not math.isfinite(ym) or not math.isfinite(nm): return None
    x1 = (ym-mid)/spread; x2=((1.0-nm)-mid)/spread; x3=(dyb-dya)/(dyb+dya+1e-9); x4=(dna-dnb)/(dna+dnb+1e-9)
    parity=(ym-(1.0-nm))/spread; fy=flow.signed_flow(y.token, lookback_seconds=flow_window); fn=-flow.signed_flow(n.token, lookback_seconds=flow_window)
    spread_ticks=max(y.spread()/y.tick,n.spread()/n.tick); depth_balance=math.log1p(dyb+dya)-math.log1p(dnb+dna)
    return ([1.0,max(-2,min(2,x1)),max(-2,min(2,x2)),max(-1,min(1,x3)),max(-1,min(1,x4)),max(-2,min(2,parity)),max(-1,min(1,fy)),max(-1,min(1,fn)),max(-3,min(3,math.log1p(spread_ticks)-1.0)),max(-3,min(3,depth_balance/5.0))],mid,spread)


def solve_weighted_ridge(rows: list[dict[str, Any]], *, ridge: float, now: int, half_life_seconds: float, category: str | None = None) -> tuple[list[float], int]:
    labeled=[row for row in rows if row.get("y") is not None and int(finite(row.get("feature_version"),0.0))==FEATURE_VERSION and isinstance(row.get("x"),list) and len(row["x"])==FEATURE_COUNT and (category is None or str(row.get("category") or "unknown")==category)]
    if len(labeled)<40: return [0.0]*FEATURE_COUNT,len(labeled)
    selected=labeled[-20000:]; p=FEATURE_COUNT; A=[[0.0]*p for _ in range(p)]; b=[0.0]*p; decay=math.log(2.0)/max(1.0,half_life_seconds)
    for row in selected:
        x=[float(value) for value in row["x"]]; spread=max(1e-6,finite(row.get("spread"),1e-3)); target=float(row["y"])/spread; age=max(0.0,now-finite(row.get("ts"),now)); weight=math.exp(-decay*age)
        for i in range(p):
            b[i]+=weight*x[i]*target
            for j in range(p): A[i][j]+=weight*x[i]*x[j]
    for i in range(1,p): A[i][i]+=ridge
    for i in range(p):
        pivot=max(range(i,p),key=lambda r:abs(A[r][i]))
        if abs(A[pivot][i])<1e-12: return [0.0]*p,len(labeled)
        A[i],A[pivot]=A[pivot],A[i]; b[i],b[pivot]=b[pivot],b[i]; diagonal=A[i][i]; A[i]=[value/diagonal for value in A[i]]; b[i]/=diagonal
        for r in range(p):
            if r==i: continue
            q=A[r][i]
            if abs(q)<1e-14: continue
            A[r]=[A[r][c]-q*A[i][c] for c in range(p)]; b[r]-=q*b[i]
    return b,len(labeled)


def cached_fee(market: Market, clob: str, cache: dict[str, Any], now: int, ttl_seconds: int = 21600) -> FeeDetails:
    saved=cache.get(market.id)
    if isinstance(saved,dict) and now-int(finite(saved.get("timestamp"),0.0))<=ttl_seconds:
        return FeeDetails(max(0.0,finite(saved.get("rate"),0.07)),max(0.0,finite(saved.get("exponent"),1.0)),bool(saved.get("taker_only",True)),bool(saved.get("verified",False)),str(saved.get("source") or "cache"))
    details=resolve_fee_details(market.raw,clob,market.condition,market.yes); cache[market.id]={"timestamp":now,"rate":details.rate,"exponent":details.exponent,"taker_only":details.taker_only,"verified":details.verified,"source":details.source}; return details


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,required=True); parser.add_argument("--run-dir",type=Path,required=True); parser.add_argument("--trade-tape",type=Path); parser.add_argument("--markets",type=int,default=250); parser.add_argument("--min-liquidity",type=float,default=25); parser.add_argument("--horizon-seconds",type=int,default=30); parser.add_argument("--max-target-staleness-seconds",type=int,default=10); parser.add_argument("--flow-lookback-seconds",type=int,default=180); parser.add_argument("--model-half-life-seconds",type=int,default=21600); parser.add_argument("--max-trade-usd",type=float,default=15); parser.add_argument("--min-edge",type=float,default=0.0003); parser.add_argument("--slippage-bps",type=float,default=5); parser.add_argument("--max-positions",type=int,default=20); parser.add_argument("--allow-unverified-fee",action="store_true"); args=parser.parse_args()
    if args.horizon_seconds<=0 or args.max_target_staleness_seconds<0: raise SystemExit("horizon and target staleness must be valid")
    cfg=json.loads(args.config.read_text(encoding="utf-8")); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; start_capital=float(cfg["starting_capital"]); max_drawdown=float(cfg.get("max_drawdown",0.15)); now=int(time.time()); args.run_dir.mkdir(parents=True,exist_ok=True); state_path=args.run_dir/"state.json"
    state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cash":start_capital,"peak":start_capital,"killed":False,"positions":{},"samples":[],"fee_cache":{}}
    cash=finite(state.get("cash"),start_capital); peak=max(start_capital,finite(state.get("peak"),start_capital)); positions=state.get("positions") if isinstance(state.get("positions"),dict) else {}; samples=state.get("samples") if isinstance(state.get("samples"),list) else []; fee_cache=state.get("fee_cache") if isinstance(state.get("fee_cache"),dict) else {}; realized_total=finite(state.get("realized_pnl_total"),0.0); failures=[]
    flow=TapeFlow.from_csv(args.trade_tape or (args.run_dir.parent/"trade_tape.csv"),lookback_seconds=max(900,args.flow_lookback_seconds),now=now)
    try: markets=discover(gamma,args.markets,args.min_liquidity); books=fetch_books(clob,markets)
    except Exception as exc: markets,books=[],{}; failures.append(f"market_data:{type(exc).__name__}:{exc}")
    current={}; by_id={market.id:market for market in markets}
    for market in markets:
        yes,no=books.get(market.yes),books.get(market.no)
        if yes and no:
            values=features(yes,no,flow,args.flow_lookback_seconds)
            if values: current[market.id]=(market,yes,no,values)
    label_stats=label_matured_samples(samples,now=now,horizon_seconds=args.horizon_seconds,max_target_staleness_seconds=args.max_target_staleness_seconds)
    global_beta,global_n=solve_weighted_ridge(samples,ridge=2e-2,now=now,half_life_seconds=args.model_half_life_seconds)
    categories={market.category for market in markets}; category_models={category:solve_weighted_ridge(samples,ridge=5e-2,now=now,half_life_seconds=args.model_half_life_seconds,category=category) for category in categories}

    def predict(market: Market,x:list[float],spread:float)->float:
        global_pred=sum(a*b for a,b in zip(global_beta,x)); local_beta,local_n=category_models.get(market.category,([0.0]*FEATURE_COUNT,0))
        if local_n>=40:
            local_pred=sum(a*b for a,b in zip(local_beta,x)); shrink=local_n/(local_n+120.0); normalized=shrink*local_pred+(1.0-shrink)*global_pred
        else: normalized=global_pred
        return max(-2.0*spread,min(2.0*spread,normalized*spread))

    slip=max(0.0,args.slippage_bps)/10000.0; realized_tick=0.0; sell_fills=0
    for market_id,position in list(positions.items()):
        current_market=current.get(market_id)
        if not current_market: continue
        market,yes,no,values=current_market; side=str(position.get("side") or "YES"); book=yes if side=="YES" else no; bid=book.bid()
        if not math.isfinite(bid): continue
        pred=predict(market,values[0],values[2]); fair_yes=max(0.001,min(0.999,values[1]+pred)); flip=(side=="YES" and fair_yes<=values[1]) or (side=="NO" and fair_yes>=values[1])
        if now-int(position["entry_ts"])>=args.horizon_seconds or flip:
            details=cached_fee(market,clob,fee_cache,now)
            if not details.verified and not args.allow_unverified_fee: continue
            price=max(1e-6,bid*(1.0-slip)); fee=fee_per_share(price,details,taker=True)*float(position["shares"]); proceeds=price*float(position["shares"])-fee; pnl=proceeds-float(position["cost"]); cash+=proceeds; realized_tick+=pnl; sell_fills+=1
            append_csv(args.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl","fee_source"],{"timestamp":now,"market_id":market_id,"slug":market.slug,"action":"SELL_TAKER","side":side,"shares":position["shares"],"price":price,"fee":fee,"pnl":pnl,"fee_source":details.source}); del positions[market_id]
    realized_total+=realized_tick

    def mark_state()->tuple[float,float]:
        mark=gross=0.0
        for market_id,position in positions.items():
            current_market=current.get(market_id)
            if not current_market: continue
            _,yes,no,_=current_market; book=yes if position["side"]=="YES" else no; bid=book.bid()
            if math.isfinite(bid): mark+=float(position["shares"])*bid; gross+=float(position["cost"])
        return mark,gross

    mark,gross=mark_state(); equity=cash+mark; peak=max(peak,equity); drawdown=max(0.0,1.0-equity/peak) if peak else 0.0; killed=bool(state.get("killed")) or drawdown>=max_drawdown
    signals=opened=0; best_edge=0.0; fee_unverified=0; ranked=[]
    if not killed and global_n>=40:
        for market_id,(market,yes,no,values) in current.items():
            if market_id in positions: continue
            pred=predict(market,values[0],values[2]); fair_yes=max(0.001,min(0.999,values[1]+pred)); details=cached_fee(market,clob,fee_cache,now)
            if not details.verified and not args.allow_unverified_fee: fee_unverified+=1; continue
            for side,book,fair in (("YES",yes,fair_yes),("NO",no,1.0-fair_yes)):
                ask=book.ask()
                if not math.isfinite(ask): continue
                entry=min(0.999999,ask*(1.0+slip)); edge=fair-entry-fee_per_share(entry,details,taker=True)
                if edge>args.min_edge: ranked.append((edge,market,side,book,fair,entry,details))
        ranked.sort(reverse=True,key=lambda row:row[0]); signals=len(ranked); best_edge=ranked[0][0] if ranked else 0.0
        for edge,market,side,book,fair,entry,details in ranked:
            if len(positions)>=args.max_positions: break
            if market.id in positions: continue
            room=min(args.max_trade_usd,max(0.0,float(cfg.get("max_market_fraction",0.025))*max(equity,1.0)),cash); shares=min(room/max(entry,1e-9),book.asks[0][1] if book.asks else 0.0)
            if shares+1e-12<book.min_order: continue
            fee=fee_per_share(entry,details,taker=True)*shares; cost=shares*entry+fee
            if cost>cash+1e-9: continue
            cash-=cost; positions[market.id]={"side":side,"shares":shares,"entry_price":entry,"entry_ts":now,"cost":cost,"fair":fair,"edge":edge,"fee_source":details.source}; opened+=1
            append_csv(args.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl","fee_source"],{"timestamp":now,"market_id":market.id,"slug":market.slug,"action":"BUY_TAKER","side":side,"shares":shares,"price":entry,"fee":fee,"pnl":0.0,"fee_source":details.source})
    for market_id,(market,yes,no,values) in current.items(): samples.append({"ts":now,"market_id":market_id,"category":market.category,"x":values[0],"mid":values[1],"spread":values[2],"y":None,"feature_version":FEATURE_VERSION})
    samples=samples[-30000:]; mark,gross=mark_state(); equity=cash+mark; peak=max(peak,equity); drawdown=max(0.0,1.0-equity/peak) if peak else 0.0; killed=killed or drawdown>=max_drawdown
    state={"timestamp":now,"cash":cash,"equity":equity,"peak":peak,"drawdown":drawdown,"killed":killed,"positions":positions,"samples":samples,"fee_cache":fee_cache,"signals":signals,"opened":opened,"best_edge":best_edge,"gross_exposure":gross,"open_positions":len(positions),"labeled_samples":sum(row.get("y") is not None for row in samples),"feature_version":FEATURE_VERSION,"global_model_samples":global_n,"category_model_samples":{key:value[1] for key,value in category_models.items()},"fee_unverified_markets":fee_unverified,"realized_pnl":realized_total,"realized_pnl_total":realized_total,"realized_pnl_last_tick":realized_tick,"buy_fills_last_tick":opened,"sell_fills_last_tick":sell_fills,"target_staleness_max_seconds":label_stats.get("max_target_staleness_seconds"),"label_stats_last_tick":label_stats,"failures":failures,"paper_only":True}
    atomic_json(state_path,state); atomic_json(args.run_dir/"status.json",state); append_csv(args.run_dir/"equity.csv",["timestamp","cash","equity","gross_exposure","open_positions","signals","best_edge","realized_pnl","drawdown","killed"],state)
    print(json.dumps({"markets":len(current),"labeled":state["labeled_samples"],"signals":signals,"opened":opened,"best_edge":best_edge,"realized_pnl":realized_total,"fee_unverified_markets":fee_unverified,"killed":killed},sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
