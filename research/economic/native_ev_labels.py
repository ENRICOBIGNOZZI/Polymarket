"""Freeze authoritative public settlement labels for native EV research."""
from __future__ import annotations
import json,math,time,urllib.parse,urllib.request
from pathlib import Path
from typing import Any,Callable,Iterable
from native_ev_dataset import iter_observations

SCHEMA="polymarket_v7_native_ev_labels_v1"

def _array(value:Any)->list:
    if isinstance(value,list): return value
    if isinstance(value,str):
        try:
            parsed=json.loads(value)
            return parsed if isinstance(parsed,list) else []
        except json.JSONDecodeError:return []
    return []

def public_market(market_id:str)->dict:
    url="https://gamma-api.polymarket.com/markets/"+urllib.parse.quote(market_id,safe="")
    req=urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-native-ev-research/1"})
    with urllib.request.urlopen(req,timeout=5) as response:
        value=json.loads(response.read().decode())
    if not isinstance(value,dict): raise ValueError("market response")
    return value

def settlement_label(market_id:str,value:dict,observed_ns:int)->dict|None:
    if value.get("closed") is not True:return None
    tokens=[str(x) for x in _array(value.get("clobTokenIds"))]
    prices=[]
    for x in _array(value.get("outcomePrices")):
        try:prices.append(float(x))
        except (TypeError,ValueError):return None
    if len(tokens)!=2 or len(prices)!=2 or not all(math.isfinite(x) for x in prices):return None
    if all(abs(x-.5)<1e-9 for x in prices): payouts={tokens[0]:.5,tokens[1]:.5}
    elif max(prices)>=1-1e-9 and min(prices)<=1e-9:
        payouts={tokens[i]:(1.0 if prices[i]>.5 else 0.0) for i in range(2)}
    else:return None
    return {"market_id":market_id,"token_payouts":payouts,"observed_ns":observed_ns,
            "source":"GAMMA_CLOSED_MARKET","closed":True}

def collect(paths:Iterable[Path],fetcher:Callable[[str],dict]=public_market,now_ns:Callable[[],int]=time.time_ns)->dict:
    markets=sorted({str(row.get("market_id") or "") for row in iter_observations(paths) if row.get("market_id")})
    labels={}; unresolved=[]
    for market in markets:
        label=settlement_label(market,fetcher(market),now_ns())
        if label is None:unresolved.append(market)
        else:labels[market]=label
    return {"schema":SCHEMA,"paper_only":True,"execution_authority":False,
            "automatic_promotion":False,"labels":labels,"unresolved_markets":unresolved,
            "observed_market_count":len(markets)}
