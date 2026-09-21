"""Fast native 2H equity grid: no model training, research only.

Uses already-recorded causal kind=6 repricing evidence. Produces hundreds of
equity curves across entry latency × exit horizon for simple diagnostic alpha
rules. Missing evidence/features are censored, never converted to fake zero PnL.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import re
import statistics

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, finite
from research.walk_forward_v3.direct_action import (
    _valid_state,
    action_side_sign,
    decision_action_sides,
    realized_action_economics,
    selected_action_side,
)
from research.walk_forward_v3.btc_compact_equity import LATENCIES, EXITS, SIZE

SCHEMA="polymarket_v7_native_equity_grid_2h_v1"
WINDOW_NS=2*60*60*1_000_000_000
STEP_NS=5*60*1_000_000_000

def feature_value(row,*names):
    source=row.get("features") or {}
    for name in names:
        value=source.get(name)
        if finite(value):
            return float(value)
    return None

def side_with_sign(row, sign):
    if sign not in (-1,1):
        return None
    for side in decision_action_sides(row):
        try:
            if action_side_sign(row,side)==sign:
                return side
        except (ValueError,KeyError,TypeError):
            continue
    return None

def direction_side(row, reverse=False):
    direction=int(row.get("direction") or 0)
    if direction not in (-1,1):
        return None,"DIRECTION_UNAVAILABLE"
    sign=-direction if reverse else direction
    side=side_with_sign(row,sign)
    return (side,None) if side else (None,"BILATERAL_SIDE_UNAVAILABLE")

def venue_side(row,venue,reverse=False):
    value=feature_value(
        row,
        f"external.{venue}_return_100ms_bp",
        f"{venue}_return_100ms_bp",
    )
    if value is None or abs(value)<=1e-15:
        return None,f"{venue.upper()}_100MS_UNAVAILABLE"
    sign=1 if value>0 else -1
    if reverse: sign=-sign
    side=side_with_sign(row,sign)
    return (side,None) if side else (None,"BILATERAL_SIDE_UNAVAILABLE")

def consensus_side(row,reverse=False):
    values=[]
    for venue in ("binance","coinbase","bybit"):
        value=feature_value(
            row,
            f"external.{venue}_return_100ms_bp",
            f"{venue}_return_100ms_bp",
        )
        if value is not None and abs(value)>1e-15:
            values.append(value)
    if len(values)<2:
        return None,"CROSS_VENUE_SUPPORT_LT2"
    signed=sum(1 if v>0 else -1 for v in values)
    if signed==0:
        return None,"CROSS_VENUE_TIE"
    sign=1 if signed>0 else -1
    if reverse: sign=-sign
    side=side_with_sign(row,sign)
    return (side,None) if side else (None,"BILATERAL_SIDE_UNAVAILABLE")

STRATEGIES={
    "BASELINE":lambda r:(selected_action_side(r),None),
    "CONTINUATION":lambda r:direction_side(r,False),
    "REVERSAL":lambda r:direction_side(r,True),
    "BINANCE_CONT":lambda r:venue_side(r,"binance",False),
    "BINANCE_REV":lambda r:venue_side(r,"binance",True),
    "COINBASE_CONT":lambda r:venue_side(r,"coinbase",False),
    "COINBASE_REV":lambda r:venue_side(r,"coinbase",True),
    "BYBIT_CONT":lambda r:venue_side(r,"bybit",False),
    "BYBIT_REV":lambda r:venue_side(r,"bybit",True),
    "CONSENSUS_CONT":lambda r:consensus_side(r,False),
    "CONSENSUS_REV":lambda r:consensus_side(r,True),
}

def native_support(row):
    arrivals=row.get("arrivals") or {}
    targets=row.get("targets") or {}
    entry=sum(str(l) in arrivals for l in LATENCIES)
    exits=sum((targets.get(str(h)) or {}).get("state")=="OBSERVED" for h in EXITS)
    return entry,exits

def candidate_windows(rows):
    ordered=sorted(rows,key=lambda r:int(r["decision_ns"]))
    if not ordered or int(ordered[-1]["decision_ns"])-int(ordered[0]["decision_ns"])<WINDOW_NS:
        return []
    first=(int(ordered[0]["decision_ns"])//STEP_NS)*STEP_NS
    last=int(ordered[-1]["decision_ns"])-WINDOW_NS
    result=[]
    for start in range(first,last+1,STEP_NS):
        end=start+WINDOW_NS
        subset=[r for r in ordered if start<=int(r["decision_ns"])<end]
        if not subset: continue
        supported=0;entry_cells=exit_cells=0
        for row in subset:
            e,x=native_support(row)
            entry_cells+=e;exit_cells+=x
            supported+=int(e>0 and x>0)
        result.append({
            "start_ns":start,"end_ns":end,"rows":subset,
            "assets":len({str(r.get("asset") or "UNKNOWN") for r in subset}),
            "contracts":len({str(r.get("horizon") or "UNKNOWN") for r in subset}),
            "markets":len({str(r["market_id"]) for r in subset}),
            "supported_rows":supported,
            "support_rate":supported/len(subset),
            "entry_label_cells":entry_cells,
            "exit_label_cells":exit_cells,
            "occupied_5m_bins":len({(int(r["decision_ns"])-start)//STEP_NS for r in subset}),
        })
    return result

def select_window(rows):
    candidates=candidate_windows(rows)
    if not candidates:
        raise ValueError("NO_TWO_HOUR_NATIVE_EVIDENCE_WINDOW")
    def score(c):
        return (
            c["assets"],c["contracts"],c["support_rate"],c["occupied_5m_bins"],
            c["markets"],c["entry_label_cells"],c["exit_label_cells"],len(c["rows"]),
            -c["start_ns"],
        )
    winner=max(candidates,key=score)
    rows_out=winner.pop("rows")
    receipt={**winner,
        "selection_rule":"DATA_QUALITY_ONLY_NATIVE_LABEL_COVERAGE_NO_PNL",
        "profitability_used_for_selection":False,
        "candidate_windows":len(candidates),
        "top_candidates":[
            {k:v for k,v in c.items() if k!="rows"}
            for c in sorted(candidates,key=score,reverse=True)[:10]
        ],
    }
    return receipt,rows_out

def execute(row,latency,horizon,side):
    return realized_action_economics(
        row,size=SIZE,horizon_ms=horizon,latency_ms=latency,side=side,
        entry_cap=.99,hard_order_notional=100.0,require_full_decision_depth=True,
    )

def equity_stats(events):
    total=peak=drawdown=0.0
    pos=neg=zero=0
    path=[]
    for event in sorted(events,key=lambda x:(x["decision_ns"],x["decision_id"])):
        pnl=float(event["cash_pnl"])
        total+=pnl;peak=max(peak,total);drawdown=max(drawdown,peak-total)
        pos+=pnl>1e-15;neg+=pnl<-1e-15;zero+=abs(pnl)<=1e-15
        path.append({"decision_ns":event["decision_ns"],"equity":total})
    fills=len(events)
    return {
        "total_pnl":total,"fills":fills,
        "pnl_per_fill":total/fills if fills else None,
        "hit_rate":pos/(pos+neg) if pos+neg else None,
        "max_drawdown":drawdown if fills else None,
        "positive":pos,"negative":neg,"zero":zero,"equity":path,
    }

def run_strategy(rows,name,selector):
    metrics={};events=defaultdict(list);censored=defaultdict(Counter)
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            observed=trades=no_fills=0
            for row in rows:
                try:
                    side,why=selector(row)
                except (ValueError,KeyError,TypeError):
                    side,why=None,"SIDE_SELECTOR_ERROR"
                if side is None:
                    censored[key][str(why or "FEATURE_UNAVAILABLE")]+=1
                    continue
                economics,state=execute(row,latency,horizon,side)
                if economics is None:
                    censored[key][state]+=1
                    continue
                observed+=1;trades+=1
                if float(economics.get("filled") or 0)>0:
                    events[key].append({
                        "decision_ns":int(row["decision_ns"]),
                        "decision_id":str(row["decision_id"]),
                        "market_id":str(row["market_id"]),
                        "asset":str(row.get("asset") or "UNKNOWN"),
                        "contract_horizon":str(row.get("horizon") or "UNKNOWN"),
                        "side":str(side),"cash_pnl":float(economics["cash_pnl"]),
                        "execution_state":state,**economics,
                    })
                else:
                    no_fills+=1
            stats=equity_stats(events[key])
            metrics[key]={
                "opportunities":len(rows),"observed_actions":observed,"trades":trades,
                "fills":stats["fills"],"no_fills":no_fills,
                "censored":len(rows)-observed,"total_pnl":stats["total_pnl"] if observed else None,
                "pnl_per_trade":stats["total_pnl"]/trades if trades else None,
                "pnl_per_fill":stats["pnl_per_fill"],"hit_rate":stats["hit_rate"],
                "max_drawdown":stats["max_drawdown"],
                "positive":stats["positive"],"negative":stats["negative"],"zero":stats["zero"],
            }
    return {
        "strategy":name,"metrics":metrics,
        "equity_events":{k:sorted(v,key=lambda x:(x["decision_ns"],x["decision_id"])) for k,v in events.items()},
        "equity_paths":{k:equity_stats(v)["equity"] for k,v in events.items()},
        "censored":{k:dict(v) for k,v in censored.items()},
    }

def safe(value):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",str(value))

def render_gallery(root,results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    gallery=root/"equity_gallery";gallery.mkdir(parents=True,exist_ok=True)
    figures=[]
    def line_group(event_map,keys,title,path):
        plt.figure(figsize=(11,5.5));drawn=False;origin=None
        for key in keys:
            seq=event_map.get(key,[])
            if not seq: continue
            if origin is None:
                origin=min(int(x["decision_ns"]) for x in seq)
            total=0.0;xs=[];ys=[]
            for row in sorted(seq,key=lambda x:(x["decision_ns"],x["decision_id"])):
                total+=float(row["cash_pnl"])
                xs.append((int(row["decision_ns"])-origin)/60_000_000_000)
                ys.append(total)
            plt.plot(xs,ys,label=key,linewidth=1.1);drawn=True
        if drawn:
            plt.axhline(0,linewidth=.8);plt.legend(fontsize=7,ncol=2)
            plt.xlabel("minutes");plt.ylabel("cumulative PnL")
        else:
            plt.text(.5,.5,"No observed fills",ha="center",va="center",transform=plt.gca().transAxes)
            plt.xticks([]);plt.yticks([])
        plt.title(title);plt.tight_layout();plt.savefig(root/path,dpi=130);plt.close()
        figures.append(str(path))

    for name,result in results.items():
        event_map=result["equity_events"];folder=gallery/safe(name);folder.mkdir(exist_ok=True)
        line_group(event_map,[f"{l}::{h}" for l in LATENCIES for h in EXITS],
                   f"{name}: all 60 entry × exit equity lines",folder.relative_to(root)/"00_all_60.png")
        for l in LATENCIES:
            line_group(event_map,[f"{l}::{h}" for h in EXITS],
                       f"{name}: entry {l}ms, all exits",folder.relative_to(root)/f"latency_{l}ms.png")
        for h in EXITS:
            line_group(event_map,[f"{l}::{h}" for l in LATENCIES],
                       f"{name}: exit {h}ms, all latencies",folder.relative_to(root)/f"exit_{h}ms.png")

    (gallery/"manifest.json").write_text(json.dumps({
        "schema":SCHEMA+"_gallery_v1","strategies":list(results),
        "latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),
        "equity_curves_per_strategy":len(LATENCIES)*len(EXITS),
        "grouped_figures_per_strategy":1+len(LATENCIES)+len(EXITS),
        "total_equity_curves":len(results)*len(LATENCIES)*len(EXITS),
        "figure_count":len(figures),"figures":figures,
    },indent=2,sort_keys=True)+"\n",encoding="utf-8")

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--code-sha",required=True)
    p.add_argument("--skip-gallery",action="store_true")
    a=p.parse_args(argv)
    out=a.output_dir;out.mkdir(parents=True,exist_ok=True)
    data=build_dataset(
        a.root,minimum_wall_ns=a.minimum_wall_ns,
        include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        raise ValueError("DATA_NOT_READY:"+str(data.get("input_state")))
    rows=[r for r in data["decisions"] if _valid_state(r)]
    receipt,window_rows=select_window(rows)
    results={name:run_strategy(window_rows,name,selector) for name,selector in STRATEGIES.items()}

    manifest={
        "schema":SCHEMA+"_manifest_v1",**SAFETY,"research_only":True,
        "automatic_promotion":False,"code_sha":a.code_sha,
        "window_start_ns":receipt["start_ns"],"window_end_ns":receipt["end_ns"],
        "window_duration_seconds":7200,"selection":receipt,
        "assets":sorted({str(r.get("asset") or "UNKNOWN") for r in window_rows}),
        "contract_horizons":sorted({str(r.get("horizon") or "UNKNOWN") for r in window_rows}),
        "rows":len(window_rows),"latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),
        "target_size_shares":SIZE,
        "evidence_source":"NATIVE_KIND6_CAUSAL_REPRICING",
        "note":"Diagnostic historical research; not exact continuous-tape baseline.",
    }
    atomic_json(out/"manifest.json",manifest)
    atomic_json(out/"equity_grid.json",{"schema":SCHEMA+"_results_v1",**SAFETY,"results":results})

    with (out/"pnl_grid.csv").open("w",newline="",encoding="utf-8") as handle:
        fields=["strategy","entry_latency_ms","exit_horizon_ms","total_pnl","fills","trades",
                "pnl_per_trade","pnl_per_fill","hit_rate","max_drawdown","censored"]
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for name,result in results.items():
            for latency in LATENCIES:
                for horizon in EXITS:
                    cell=result["metrics"][f"{latency}::{horizon}"]
                    writer.writerow({
                        "strategy":name,"entry_latency_ms":latency,"exit_horizon_ms":horizon,
                        **{k:cell.get(k) for k in fields[3:]},
                    })

    if not a.skip_gallery:
        render_gallery(out,results)

    summary=[]
    for name,result in results.items():
        valid=[(k,v) for k,v in result["metrics"].items() if finite(v.get("total_pnl"))]
        best=max(valid,key=lambda kv:float(kv[1]["total_pnl"])) if valid else (None,None)
        summary.append({
            "strategy":name,"best_cell":best[0],
            "best_total_pnl":None if best[1] is None else best[1]["total_pnl"],
            "best_fills":None if best[1] is None else best[1]["fills"],
        })
    atomic_json(out/"summary.json",{"schema":SCHEMA+"_summary_v1",**SAFETY,"strategies":summary})
    print(json.dumps({"state":"READY","window":receipt,"strategies":summary},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
