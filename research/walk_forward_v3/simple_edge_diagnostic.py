"""Fast causal diagnostic for the simple external-signal trading idea.

No ML fitting and no policy promotion. Every admissible decision is evaluated at
venue-minimum-or-5-shares across a fixed latency/exit grid, then summarized on
chronologically separated discovery and validation market halves.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset
from research.walk_forward_v3.direct_action import (
    DEFAULT_HARD_ORDER_NOTIONAL,
    _valid_state,
    decision_side_state,
    realized_action_economics,
    selected_action_side,
)

SCHEMA="polymarket_v7_simple_edge_diagnostic_v1"
LATENCIES=(25,50,100)
EXITS=(250,500,1000,2000)


def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v))


def signal_bp(row):
    features=row.get("features") or {}
    for key in (
        "external.binance_return_100ms_bp",
        "binance_return_100ms_bp",
        "signal_return_bp",
    ):
        if finite(features.get(key)):
            return float(features[key])
    return None


def bucket_signal(value):
    if value is None: return "missing"
    value=abs(float(value))
    if value < .25: return "lt0.25"
    if value < .50: return "0.25_0.50"
    if value < 1.0: return "0.50_1.00"
    if value < 2.0: return "1.00_2.00"
    return "ge2.00"


def bucket_price(value):
    value=float(value)
    if value < .25: return "lt0.25"
    if value < .50: return "0.25_0.50"
    if value < .75: return "0.50_0.75"
    return "ge0.75"


def bucket_tte(ns):
    seconds=float(ns)/1e9
    if seconds < 60: return "30_60"
    if seconds < 90: return "60_90"
    if seconds < 105: return "90_105"
    return "105_120"


def market_halves(rows):
    first={}
    for row in rows:
        market=str(row.get("market_id") or "")
        ts=int(row.get("decision_ns") or 0)
        if market and ts:
            first[market]=min(first.get(market,ts),ts)
    ordered=sorted(first,key=lambda m:(first[m],m))
    cut=max(1,len(ordered)//2)
    return set(ordered[:cut]),set(ordered[cut:])


def fresh_stats():
    return {
        "actions":0,"fills":0,"full_fills":0,"partial_fills":0,"no_fills":0,
        "positive":0,"zero":0,"negative":0,
        "pnl":0.0,"turnover":0.0,
    }


def add(stats,economics,state):
    stats["actions"]+=1
    pnl=float(economics["cash_pnl"])
    stats["pnl"]+=pnl
    if pnl>1e-15: stats["positive"]+=1
    elif pnl<-1e-15: stats["negative"]+=1
    else: stats["zero"]+=1
    if state=="OBSERVED_FULL_FILL":
        stats["fills"]+=1; stats["full_fills"]+=1
    elif state=="OBSERVED_PARTIAL_FILL":
        stats["fills"]+=1; stats["partial_fills"]+=1
    else:
        stats["no_fills"]+=1
    filled=float(economics.get("filled") or 0.0)
    price=economics.get("entry_price")
    if filled>0 and finite(price):
        stats["turnover"]+=filled*float(price)


def finalize(stats):
    out=dict(stats)
    n=stats["actions"]
    fills=stats["fills"]
    out["mean_pnl_per_action"]=stats["pnl"]/n if n else None
    out["pnl_per_fill"]=stats["pnl"]/fills if fills else None
    out["fill_rate"]=fills/n if n else None
    out["positive_rate"]=stats["positive"]/n if n else None
    out["pnl_per_dollar_turnover"]=(
        stats["pnl"]/stats["turnover"] if stats["turnover"]>0 else None)
    return out


def analyze(records):
    rows=[row for row in records if _valid_state(row)]
    discovery,validation=market_halves(rows)
    groups={
        split:defaultdict(fresh_stats)
        for split in ("DISCOVERY","VALIDATION","ALL")
    }
    unavailable={
        split:Counter() for split in ("DISCOVERY","VALIDATION","ALL")
    }
    decision_counts=Counter()

    for row in rows:
        market=str(row["market_id"])
        split="DISCOVERY" if market in discovery else "VALIDATION"
        side=selected_action_side(row)
        state=decision_side_state(row,side)
        if state is None:
            continue
        minimum=float(row["minimum"])
        size=max(5.0,minimum)
        ask=float(state["ask"])
        depth=float(state["ask_quantity"])
        if (
            size>depth+1e-12
            or size*ask>DEFAULT_HARD_ORDER_NOTIONAL+1e-9
            or ask>.99
        ):
            unavailable[split]["DECISION_CAPACITY_OR_PRICE"]+=1
            unavailable["ALL"]["DECISION_CAPACITY_OR_PRICE"]+=1
            continue
        sig=signal_bp(row)
        dimensions={
            "overall":"ALL",
            "asset":str(row["asset"]),
            "contract":str(row["horizon"]),
            "signal":bucket_signal(sig),
            "price":bucket_price(ask),
            "tte":bucket_tte(row["tte_ns"]),
        }
        decision_counts[(split,"eligible_decisions")]+=1
        decision_counts[("ALL","eligible_decisions")]+=1

        for latency in LATENCIES:
            for horizon in EXITS:
                if horizon<=latency: continue
                economics,why=realized_action_economics(
                    row,size=size,horizon_ms=horizon,latency_ms=latency,
                    side=side,entry_cap=.99,
                    hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                )
                if economics is None:
                    unavailable[split][str(why or "UNAVAILABLE")]+=1
                    unavailable["ALL"][str(why or "UNAVAILABLE")]+=1
                    continue
                keys=[f"latency_exit::{latency}::{horizon}"]
                keys.extend(
                    f"{name}::{value}::{latency}::{horizon}"
                    for name,value in dimensions.items()
                )
                for key in keys:
                    add(groups[split][key],economics,why)
                    add(groups["ALL"][key],economics,why)

    result={}
    for split,table in groups.items():
        result[split]={
            key:finalize(value) for key,value in sorted(table.items())
        }
    return {
        "schema":SCHEMA,
        **SAFETY,
        "state":"READY",
        "diagnostic_only":True,
        "automatic_promotion":False,
        "strategy":"SIGNAL_DIRECTION_FIXED_SMALL_SIZE_NO_ML",
        "size_semantics":"MAX_5_SHARES_OR_VENUE_MINIMUM;L1_CAPACITY_BOUNDED",
        "entry_cap":.99,
        "latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),
        "discovery_validation_split":"WHOLE_MARKET_CHRONOLOGICAL_HALF",
        "valid_decision_rows":len(rows),
        "discovery_markets":len(discovery),
        "validation_markets":len(validation),
        "eligible_decisions":{
            split:int(decision_counts[(split,"eligible_decisions")])
            for split in ("DISCOVERY","VALIDATION","ALL")
        },
        "unavailable_reasons":{
            split:dict(counter.most_common())
            for split,counter in unavailable.items()
        },
        "groups":result,
    }


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--minimum-wall-ns",type=int,required=True)
    args=parser.parse_args(argv)
    data=build_dataset(
        args.root,
        minimum_wall_ns=args.minimum_wall_ns,
        include_settlement_labels=False,
        use_compact_window_index=True,
    )
    if data.get("input_state")!="READY":
        value={
            "schema":SCHEMA,**SAFETY,"state":data.get("input_state"),
            "diagnostic_only":True,"automatic_promotion":False,
            "data_sha256":data.get("data_sha256"),
        }
    else:
        value=analyze(data["decisions"])
        value["data_sha256"]=data.get("data_sha256")
    atomic_json(args.output,value)
    return 0 if value.get("state")=="READY" else 2


if __name__=="__main__":
    raise SystemExit(main())
