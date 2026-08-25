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
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent":"polymarket-v6-paper/1","Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list): return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except json.JSONDecodeError: return []
    return []


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        if not exists: w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not 0.0 < price < 1.0 or rate <= 0: return 0.0
    return rate * (price * (1.0 - price)) ** max(0.0, exponent)


class Market:
    def __init__(self, raw: dict[str, Any]):
        ids=[str(x) for x in parse_array(raw.get("clobTokenIds"))]; outcomes=[str(x).lower() for x in parse_array(raw.get("outcomes"))]
        if len(ids)<2: raise ValueError
        yi,ni=0,1
        for i,x in enumerate(outcomes[:len(ids)]):
            if x=="yes": yi=i
            elif x=="no": ni=i
        self.id=str(raw.get("id") or ""); self.condition=str(raw.get("conditionId") or ""); self.event=str(raw.get("eventId") or self.condition or self.id)
        events=raw.get("events")
        if isinstance(events,list) and events and isinstance(events[0],dict): self.event=str(events[0].get("id") or self.event)
        self.slug=str(raw.get("slug") or self.id); self.yes=ids[yi]; self.no=ids[ni]; self.liq=max(0.0,finite(raw.get("liquidityNum"),0.0))
        fs=raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"),dict) else {}
        self.fee_rate=max(0.0,finite(fs.get("rate"),0.07)); self.fee_exp=max(0.0,finite(fs.get("exponent"),1.0))


class Book:
    def __init__(self, raw: dict[str, Any]):
        self.token=str(raw.get("asset_id") or ""); self.tick=max(1e-6,finite(raw.get("tick_size"),0.01)); self.min_order=max(1.0,finite(raw.get("min_order_size"),1.0))
        self.bids=[]; self.asks=[]
        for z in raw.get("bids",[]):
            if isinstance(z,dict):
                p,q=finite(z.get("price")),finite(z.get("size"),0.0)
                if math.isfinite(p) and 0<p<1 and q>0:self.bids.append((p,q))
        for z in raw.get("asks",[]):
            if isinstance(z,dict):
                p,q=finite(z.get("price")),finite(z.get("size"),0.0)
                if math.isfinite(p) and 0<p<1 and q>0:self.asks.append((p,q))
        self.bids.sort(reverse=True); self.asks.sort()
    def bid(self): return self.bids[0][0] if self.bids else math.nan
    def ask(self): return self.asks[0][0] if self.asks else math.nan
    def mid(self):
        b,a=self.bid(),self.ask(); return .5*(a+b) if math.isfinite(a) and math.isfinite(b) else math.nan
    def spread(self):
        b,a=self.bid(),self.ask(); return a-b if math.isfinite(a) and math.isfinite(b) else math.nan
    def depth(self,bid_side:bool,n:int=5):
        levels=self.bids if bid_side else self.asks
        if not levels:return 0.0
        best=levels[0][0]; scale=max(1e-4,3*self.tick)
        return sum(q*math.exp(-abs(p-best)/scale) for p,q in levels[:n])
    def micro(self):
        b,a=self.bid(),self.ask(); db,da=self.depth(True),self.depth(False)
        if not math.isfinite(b) or not math.isfinite(a):return math.nan
        return (a*db+b*da)/(db+da) if db+da>1e-12 else .5*(a+b)


def discover(gamma:str,limit:int,min_liq:float)->list[Market]:
    out=[]; offset=0
    while len(out)<limit and offset<4000:
        qs=urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        raw=request_json(gamma+"/markets?"+qs); batch=raw if isinstance(raw,list) else raw.get("markets",[]) if isinstance(raw,dict) else []
        if not batch:break
        for z in batch:
            if not isinstance(z,dict):continue
            try:m=Market(z)
            except ValueError:continue
            if m.id and m.condition and m.liq>=min_liq:out.append(m)
            if len(out)>=limit:break
        if len(batch)<100:break
        offset+=100
    return out


def fetch_books(clob:str,markets:list[Market])->dict[str,Book]:
    tokens=[t for m in markets for t in (m.yes,m.no)]; out={}
    for i in range(0,len(tokens),80):
        raw=request_json(clob+"/books",[{"token_id":x} for x in tokens[i:i+80]])
        for z in raw if isinstance(raw,list) else []:
            if not isinstance(z,dict):continue
            b=Book(z)
            if b.token and b.bids and b.asks:out[b.token]=b
    return out


def features(y:Book,n:Book)->tuple[list[float],float,float] | None:
    mid=y.mid(); spread=max(y.spread(),n.spread())
    if not math.isfinite(mid) or not math.isfinite(spread) or spread<=0:return None
    ym=y.micro(); nm=n.micro(); dyb,dya=y.depth(True),y.depth(False); dnb,dna=n.depth(True),n.depth(False)
    if not math.isfinite(ym) or not math.isfinite(nm):return None
    x1=(ym-mid)/spread
    x2=((1.0-nm)-mid)/spread
    x3=(dyb-dya)/(dyb+dya+1e-9)
    x4=(dna-dnb)/(dna+dnb+1e-9)
    parity=(ym-(1.0-nm))/spread
    return [1.0,max(-2,min(2,x1)),max(-2,min(2,x2)),max(-1,min(1,x3)),max(-1,min(1,x4)),max(-2,min(2,parity))],mid,spread


def solve_ridge(rows:list[dict[str,Any]],ridge:float)->list[float]:
    labeled=[r for r in rows if r.get("y") is not None]
    if len(labeled)<40:return [0.0]*6
    p=6; A=[[0.0]*p for _ in range(p)]; b=[0.0]*p
    for r in labeled[-10000:]:
        x=[float(v) for v in r["x"]]; y=float(r["y"])
        for i in range(p):
            b[i]+=x[i]*y
            for j in range(p):A[i][j]+=x[i]*x[j]
    for i in range(1,p):A[i][i]+=ridge
    for i in range(p):
        pivot=max(range(i,p),key=lambda r:abs(A[r][i]))
        if abs(A[pivot][i])<1e-12:return [0.0]*p
        A[i],A[pivot]=A[pivot],A[i]; b[i],b[pivot]=b[pivot],b[i]
        d=A[i][i]; A[i]=[v/d for v in A[i]]; b[i]/=d
        for r in range(p):
            if r==i:continue
            q=A[r][i]
            if abs(q)<1e-14:continue
            A[r]=[A[r][c]-q*A[i][c] for c in range(p)]; b[r]-=q*b[i]
    return b


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path,required=True); ap.add_argument("--run-dir",type=Path,required=True); ap.add_argument("--markets",type=int,default=250); ap.add_argument("--min-liquidity",type=float,default=25); ap.add_argument("--horizon-seconds",type=int,default=30); ap.add_argument("--max-trade-usd",type=float,default=15); ap.add_argument("--min-edge",type=float,default=.0003); ap.add_argument("--slippage-bps",type=float,default=5); ap.add_argument("--max-positions",type=int,default=20); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; start_cap=float(cfg["starting_capital"]); max_dd=float(cfg.get("max_drawdown",.15)); now=int(time.time()); args.run_dir.mkdir(parents=True,exist_ok=True)
    state_path=args.run_dir/"state.json"; state=json.loads(state_path.read_text()) if state_path.exists() else {"cash":start_cap,"peak":start_cap,"killed":False,"positions":{},"samples":[]}
    cash=finite(state.get("cash"),start_cap); peak=max(start_cap,finite(state.get("peak"),start_cap)); positions=state.get("positions") if isinstance(state.get("positions"),dict) else {}; samples=state.get("samples") if isinstance(state.get("samples"),list) else []
    failures=[]
    try: markets=discover(gamma,args.markets,args.min_liquidity); books=fetch_books(clob,markets)
    except Exception as exc: markets=[]; books={}; failures.append(f"market_data:{type(exc).__name__}:{exc}")
    by_id={m.id:m for m in markets}; current={}
    for m in markets:
        y,n=books.get(m.yes),books.get(m.no)
        if y and n:
            z=features(y,n)
            if z:current[m.id]=(m,y,n,z)
    # Label matured samples at first observable mark after the forecast horizon.
    for r in samples:
        if r.get("y") is not None or now-int(r.get("ts",0))<args.horizon_seconds:continue
        cur=current.get(str(r.get("market_id") or ""))
        if cur:r["y"]=cur[3][1]-float(r["mid"])
    beta=solve_ridge(samples,1e-2)
    slip=max(0.0,args.slippage_bps)/10000.0
    realized=0.0
    # Mark and exit fixed-horizon positions. Short holding periods prevent a micro
    # forecast from silently turning into a terminal event bet.
    for mid,p in list(positions.items()):
        cur=current.get(mid)
        if not cur:continue
        m,y,n,z=cur; side=p["side"]; book=y if side=="YES" else n; bid=book.bid()
        if not math.isfinite(bid):continue
        pred=sum(a*b for a,b in zip(beta,z[0])); pred=max(-2*z[2],min(2*z[2],pred))
        fair=z[1]+pred; flip=(side=="YES" and fair<=z[1]) or (side=="NO" and fair>=z[1])
        if now-int(p["entry_ts"])>=args.horizon_seconds or flip:
            px=max(1e-6,bid*(1-slip)); fee=fee_per_share(px,m.fee_rate,m.fee_exp)*float(p["shares"]); proceeds=px*float(p["shares"])-fee; pnl=proceeds-float(p["cost"]); cash+=proceeds; realized+=pnl
            append_csv(args.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl"],{"timestamp":now,"market_id":mid,"slug":m.slug,"action":"SELL","side":side,"shares":p["shares"],"price":px,"fee":fee,"pnl":pnl}); del positions[mid]
    equity=cash
    for mid,p in positions.items():
        cur=current.get(mid)
        if cur:
            book=cur[1] if p["side"]=="YES" else cur[2]; bid=book.bid(); equity+=float(p["shares"])*(bid if math.isfinite(bid) else float(p["entry_price"]))
        else:equity+=float(p["shares"])*float(p["entry_price"])
    peak=max(peak,equity); drawdown=max(0.0,1-equity/peak) if peak>0 else 0.0; killed=bool(state.get("killed")) or drawdown>=max_dd
    signals=0; opened=0; best_edge=0.0
    if not killed and len([r for r in samples if r.get("y") is not None])>=40:
        ranked=[]
        for mid,(m,y,n,z) in current.items():
            if mid in positions:continue
            pred=sum(a*b for a,b in zip(beta,z[0])); pred=max(-2*z[2],min(2*z[2],pred)); fair=max(.001,min(.999,z[1]+pred))
            for side,book,q in (("YES",y,fair),("NO",n,1-fair)):
                ask=book.ask()
                if not math.isfinite(ask):continue
                entry=min(.999999,ask*(1+slip)); fee=fee_per_share(entry,m.fee_rate,m.fee_exp); edge=q-entry-fee
                if edge>args.min_edge:ranked.append((edge,m,side,book,q,entry,fee))
        ranked.sort(reverse=True,key=lambda x:x[0]); signals=len(ranked); best_edge=ranked[0][0] if ranked else 0.0
        for edge,m,side,book,q,entry,fee_ps in ranked:
            if len(positions)>=args.max_positions:break
            if m.id in positions:continue
            room=max(0.0,min(args.max_trade_usd,0.025*equity,cash)); shares=room/max(entry+fee_ps,1e-6)
            if shares<book.min_order:continue
            fee=fee_ps*shares; cost=entry*shares+fee
            if cost>cash:continue
            positions[m.id]={"side":side,"shares":shares,"entry_price":entry,"cost":cost,"entry_ts":now}; cash-=cost; opened+=1
            append_csv(args.run_dir/"fills.csv",["timestamp","market_id","slug","action","side","shares","price","fee","pnl"],{"timestamp":now,"market_id":m.id,"slug":m.slug,"action":"BUY","side":side,"shares":shares,"price":entry,"fee":fee,"pnl":0.0})
    # Record current features after trading decision, then retain a bounded online calibration set.
    for mid,(m,y,n,z) in current.items():samples.append({"ts":now,"market_id":mid,"mid":z[1],"spread":z[2],"x":z[0],"y":None})
    samples=samples[-20000:]
    equity=cash
    for mid,p in positions.items():
        cur=current.get(mid); book=(cur[1] if p["side"]=="YES" else cur[2]) if cur else None; bid=book.bid() if book else float(p["entry_price"]); equity+=float(p["shares"])*(bid if math.isfinite(bid) else float(p["entry_price"]))
    peak=max(peak,equity); drawdown=max(0.0,1-equity/peak) if peak>0 else 0.0; killed=killed or drawdown>=max_dd
    state={"timestamp":now,"cash":cash,"equity":equity,"peak":peak,"drawdown":drawdown,"killed":killed,"positions":positions,"samples":samples,"beta":beta,"labeled_samples":sum(r.get("y") is not None for r in samples),"signals":signals,"opened":opened,"best_edge":best_edge,"realized_pnl_last_tick":realized,"failures":failures}
    atomic_json(state_path,state); atomic_json(args.run_dir/"status.json",{"cash":cash,"equity":equity,"peak_equity":peak,"drawdown":drawdown,"gross_exposure":sum(float(p["cost"]) for p in positions.values()),"open_positions":len(positions),"killed":killed,"realized_pnl":realized,"signals":signals,"opened":opened,"best_edge":best_edge,"labeled_samples":state["labeled_samples"]})
    append_csv(args.run_dir/"equity.csv",["timestamp","cash","equity","drawdown","open_positions","signals","opened","best_edge","labeled_samples"],{"timestamp":now,"cash":cash,"equity":equity,"drawdown":drawdown,"open_positions":len(positions),"signals":signals,"opened":opened,"best_edge":best_edge,"labeled_samples":state["labeled_samples"]})
    print(json.dumps({"markets":len(markets),"labeled":state["labeled_samples"],"signals":signals,"opened":opened,"positions":len(positions),"equity":equity,"best_edge":best_edge,"killed":killed},sort_keys=True)); return 0


if __name__=="__main__": raise SystemExit(main())
