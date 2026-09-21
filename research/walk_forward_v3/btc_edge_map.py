"""BTC-only causal edge map for fast PAPER research.

No ML, no promotion, no live activation. Evaluates fixed small share sizes,
causal execution latencies and exits, plus every-decision versus one-entry-per-
signal-version frequency semantics on chronological discovery/validation halves.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
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

SCHEMA="polymarket_v7_btc_edge_map_v1"
LATENCIES=(25,50,100,250)
EXITS=(500,1000,2000)
TARGET_SIZES=(5.0,10.0,20.0,40.0)
MODES=("EVERY_DECISION","ONE_PER_SIGNAL_VERSION")


def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v))


def signal_bp(row):
    f=row.get("features") or {}
    for key in ("external.binance_return_100ms_bp","binance_return_100ms_bp","signal_return_bp"):
        if finite(f.get(key)):
            return float(f[key])
    return None


def signal_bucket(v):
    if v is None: return "missing"
    x=abs(float(v))
    if x < .25: return "lt0.25"
    if x < .50: return "0.25_0.50"
    if x < 1.0: return "0.50_1.00"
    if x < 2.0: return "1.00_2.00"
    return "ge2.00"


def price_bucket(v):
    v=float(v)
    if v < .25: return "lt0.25"
    if v < .50: return "0.25_0.50"
    if v < .75: return "0.50_0.75"
    return "ge0.75"


def market_halves(rows):
    first={}
    for row in rows:
        market=str(row["market_id"])
        first[market]=min(first.get(market,int(row["decision_ns"])),int(row["decision_ns"]))
    ordered=sorted(first,key=lambda m:(first[m],m))
    cut=max(1,len(ordered)//2)
    return set(ordered[:cut]),set(ordered[cut:])


def fresh():
    return {
        "actions":0,"fills":0,"positive":0,"zero":0,"negative":0,
        "pnl":0.0,"turnover":0.0,"notional_requested":0.0,
    }


def add(stats,economics,state,size,ask):
    stats["actions"]+=1
    pnl=float(economics["cash_pnl"])
    stats["pnl"]+=pnl
    stats["notional_requested"]+=float(size)*float(ask)
    if pnl>1e-15: stats["positive"]+=1
    elif pnl<-1e-15: stats["negative"]+=1
    else: stats["zero"]+=1
    if state in ("OBSERVED_FULL_FILL","OBSERVED_PARTIAL_FILL"):
        stats["fills"]+=1
    filled=float(economics.get("filled") or 0.0)
    price=economics.get("entry_price")
    if filled>0 and finite(price):
        stats["turnover"]+=filled*float(price)


def finish(stats,span_hours):
    out=dict(stats)
    n=stats["actions"]; fills=stats["fills"]
    out["fill_rate"]=fills/n if n else None
    out["mean_pnl_per_action"]=stats["pnl"]/n if n else None
    out["pnl_per_fill"]=stats["pnl"]/fills if fills else None
    out["pnl_per_dollar_turnover"]=stats["pnl"]/stats["turnover"] if stats["turnover"]>0 else None
    out["actions_per_hour"]=n/span_hours if span_hours and span_hours>0 else None
    out["pnl_per_hour"]=stats["pnl"]/span_hours if span_hours and span_hours>0 else None
    return out


def span_hours(rows):
    stamps=[int(r["decision_ns"]) for r in rows]
    if len(stamps)<2: return None
    span=max(stamps)-min(stamps)
    return span/3_600_000_000_000 if span>0 else None


def fixed_mode_rows(rows,mode):
    ordered=sorted(rows,key=lambda r:(r["decision_ns"],r["decision_id"]))
    if mode=="EVERY_DECISION":
        return ordered
    used=set(); out=[]
    for row in ordered:
        # signal_version is producer-side causal signal identity. This is the
        # fail-closed proxy available in the immutable native tape.
        key=(
            str(row.get("market_id") or ""),
            str(row.get("capture_id") or ""),
            int(row.get("signal_version") or 0),
        )
        if key[2] <= 0 or key in used:
            continue
        used.add(key); out.append(row)
    return out


def analyze(records):
    rows=[r for r in records if _valid_state(r) and str(r.get("asset"))=="BTC"]
    discovery,validation=market_halves(rows)
    result={
        "schema":SCHEMA,**SAFETY,
        "state":"READY","diagnostic_only":True,"automatic_promotion":False,
        "asset":"BTC",
        "latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),
        "target_sizes_shares":list(TARGET_SIZES),
        "frequency_modes":list(MODES),
        "shock_identity":"MARKET_CAPTURE_SIGNAL_VERSION",
        "discovery_validation_split":"WHOLE_MARKET_CHRONOLOGICAL_HALF",
        "discovery_markets":len(discovery),"validation_markets":len(validation),
        "splits":{},
    }

    for split,markets in (("DISCOVERY",discovery),("VALIDATION",validation)):
        split_rows=[r for r in rows if str(r["market_id"]) in markets]
        split_hours=span_hours(split_rows)
        split_out={
            "decision_rows":len(split_rows),
            "span_hours":split_hours,
            "modes":{},
        }
        for mode in MODES:
            mode_rows=fixed_mode_rows(split_rows,mode)
            table=defaultdict(fresh)
            unavailable=defaultdict(int)
            for row in mode_rows:
                side=selected_action_side(row)
                state=decision_side_state(row,side)
                if state is None:
                    unavailable["SIDE_STATE_UNAVAILABLE"]+=1
                    continue
                ask=float(state["ask"])
                depth=float(state["ask_quantity"])
                sig=signal_bucket(signal_bp(row))
                price=price_bucket(ask)
                contract=str(row["horizon"])
                side_name=str(side)
                for target_size in TARGET_SIZES:
                    minimum=float(row["minimum"])
                    if minimum>target_size+1e-12:
                        unavailable["VENUE_MINIMUM_ABOVE_TARGET_SIZE"]+=1
                        continue
                    size=float(target_size)
                    if size>depth+1e-12 or size*ask>DEFAULT_HARD_ORDER_NOTIONAL+1e-9 or ask>.99:
                        unavailable["SIZE_DEPTH_OR_NOTIONAL_CAP"]+=1
                        continue
                    for latency in LATENCIES:
                        for exit_ms in EXITS:
                            if exit_ms<=latency:
                                continue
                            economics,why=realized_action_economics(
                                row,size=size,horizon_ms=exit_ms,
                                latency_ms=latency,side=side,
                                entry_cap=.99,
                                hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                            )
                            if economics is None:
                                unavailable[str(why or "UNAVAILABLE")]+=1
                                continue
                            suffix=f"{latency}::{exit_ms}::{int(target_size)}"
                            keys=(
                                f"overall::{suffix}",
                                f"contract::{contract}::{suffix}",
                                f"side::{side_name}::{suffix}",
                                f"signal::{sig}::{suffix}",
                                f"price::{price}::{suffix}",
                                f"contract_side::{contract}::{side_name}::{suffix}",
                                f"contract_signal::{contract}::{sig}::{suffix}",
                                f"side_signal::{side_name}::{sig}::{suffix}",
                                f"side_price::{side_name}::{price}::{suffix}",
                            )
                            for key in keys:
                                add(table[key],economics,why,size,ask)
            split_out["modes"][mode]={
                "candidate_rows":len(mode_rows),
                "unavailable":dict(sorted(unavailable.items())),
                "cells":{
                    key:finish(value,split_hours)
                    for key,value in sorted(table.items())
                },
            }
        result["splits"][split]=split_out

    # Frozen diagnostic shortlist: require same exact cell positive in both
    # halves with minimum support. This is a research summary, never promotion.
    candidates=[]
    for mode in MODES:
        disc=result["splits"]["DISCOVERY"]["modes"][mode]["cells"]
        val=result["splits"]["VALIDATION"]["modes"][mode]["cells"]
        for key,v in val.items():
            d=disc.get(key)
            if not d: continue
            if (
                int(d.get("actions") or 0)>=8
                and int(v.get("actions") or 0)>=8
                and float(d.get("pnl") or 0)>0
                and float(v.get("pnl") or 0)>0
            ):
                candidates.append({
                    "mode":mode,"cell":key,
                    "discovery":d,"validation":v,
                    "combined_pnl":float(d["pnl"])+float(v["pnl"]),
                })
    candidates.sort(key=lambda x:(x["combined_pnl"],x["validation"]["pnl"]),reverse=True)
    result["consistent_positive_cells"]=candidates[:200]
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    data=build_dataset(
        a.root,
        minimum_wall_ns=a.minimum_wall_ns,
        include_settlement_labels=False,
        use_compact_window_index=True,
    )
    if data.get("input_state")!="READY":
        out={
            "schema":SCHEMA,**SAFETY,"state":data.get("input_state"),
            "diagnostic_only":True,"automatic_promotion":False,
            "data_sha256":data.get("data_sha256"),
            "compact_window_index":data.get("compact_window_index"),
        }
    else:
        out=analyze(data["decisions"])
        out["data_sha256"]=data.get("data_sha256")
        out["compact_window_index"]=data.get("compact_window_index")
    atomic_json(a.output,out)
    return 0 if out.get("state")=="READY" else 2


if __name__=="__main__":
    raise SystemExit(main())
