"""Generate the required 2H multi-alpha research figures from frozen JSON outputs."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

LATENCIES=(5,10,25,50,100,250)
EXITS=(500,750,1000,1500,2000,3000,4000,5000,7500,10000)
DELAYS=(0,1,2,5,10,25,50,100,250,500,1000)

def load(root,name):
    return json.loads((root/name).read_text(encoding="utf-8"))

def best_cell(metrics):
    rows=[(k,v) for k,v in metrics.items() if isinstance(v,dict) and isinstance(v.get("total_pnl"),(int,float))]
    return max(rows,key=lambda kv:float(kv[1]["total_pnl"])) if rows else (None,None)

def equity(events):
    total=0.0;xs=[];ys=[]
    for row in sorted(events,key=lambda r:(int(r["decision_ns"]),str(r["decision_id"]))):
        total+=float(row["cash_pnl"]);xs.append(int(row["decision_ns"]));ys.append(total)
    return xs,ys

def save(root,name):
    plt.tight_layout()
    plt.savefig(root/name,dpi=150,bbox_inches="tight")
    plt.close()

def heatmap(root,name,title,metrics,delta=False):
    matrix=[]
    for latency in LATENCIES:
        row=[]
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            value=metrics.get(key)
            if delta:
                row.append(float(value) if isinstance(value,(int,float)) else np.nan)
            else:
                row.append(float((value or {}).get("total_pnl")) if isinstance((value or {}).get("total_pnl"),(int,float)) else np.nan)
        matrix.append(row)
    plt.figure(figsize=(11,4.5))
    image=plt.imshow(np.asarray(matrix),aspect="auto",origin="lower")
    plt.colorbar(image,label="PnL")
    plt.xticks(range(len(EXITS)),[str(x) for x in EXITS],rotation=60,ha="right")
    plt.yticks(range(len(LATENCIES)),[str(x) for x in LATENCIES])
    plt.xlabel("exit horizon ms");plt.ylabel("entry latency ms");plt.title(title)
    save(root,name)

def annotate_empty(text):
    plt.text(.5,.5,text,ha="center",va="center",transform=plt.gca().transAxes)
    plt.xticks([]);plt.yticks([])

def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",str(value))

def plot_equity_group(root, event_map, keys, title, filename):
    plt.figure(figsize=(11,5.5))
    drawn=False
    for key in keys:
        events=event_map.get(key,[])
        x,y=equity(events)
        if not x:
            continue
        origin=min(x)
        xm=[(v-origin)/60_000_000_000 for v in x]
        plt.plot(xm,y,label=key,linewidth=1.2)
        drawn=True
    if drawn:
        plt.axhline(0,linewidth=.8)
        plt.legend(fontsize=7,ncol=2)
        plt.xlabel("minutes since first fill")
        plt.ylabel("cumulative PnL")
    else:
        annotate_empty("No observed fills for this group")
    plt.title(title)
    save(root,filename)
    return drawn

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    a=p.parse_args();root=a.root
    baseline=load(root,"04_baseline_2h.json")["full_2h"]
    nested=load(root,"08_nested_models.json").get("models",{})
    info=load(root,"09_information_latency.json").get("curves",{})
    signal=load(root,"11_signal_decay.json").get("curves",{})
    actions=load(root,"12_momentum_reversal.json").get("actions",{})
    dynamic=load(root,"13_dynamic_exit.json")
    scorecard=load(root,"16_feature_scorecard.json").get("rows",[])

    bkey,_=best_cell(baseline["metrics"])
    ranked=[]
    for family,value in nested.items():
        score=(value.get("robustness") or {}).get("median_delta_pnl")
        if isinstance(score,(int,float)):
            ranked.append((float(score),family))
    ranked.sort(reverse=True)
    rich_family=ranked[0][1] if ranked else None
    rich=(nested.get(rich_family) or {}).get("local_test") if rich_family else None
    rkey,_=best_cell((rich or {}).get("metrics",{}))
    delta=(nested.get(rich_family) or {}).get("delta_vs_exact_baseline_local_test",{}) if rich_family else {}

    plt.figure(figsize=(9,4))
    b_events=(baseline.get("equity_events") or {}).get(bkey,[])
    x,y=equity(b_events)
    if x: plt.plot(x,y)
    else: annotate_empty("No baseline fills")
    plt.title(f"Baseline 2H equity — {bkey}");plt.xlabel("decision ns");plt.ylabel("PnL")
    save(root,"01_baseline_equity.png")

    plt.figure(figsize=(9,4))
    r_events=((rich or {}).get("events") or {}).get(rkey,[])
    x2,y2=equity(r_events)
    if x2: plt.plot(x2,y2)
    else: annotate_empty("No enriched fills / insufficient data")
    plt.title(f"Best enriched 2H equity — {rich_family} {rkey}");plt.xlabel("decision ns");plt.ylabel("PnL")
    save(root,"02_best_enriched_equity.png")

    plt.figure(figsize=(9,4))
    if x: plt.plot(x,y,label="baseline")
    if x2: plt.plot(x2,y2,label=str(rich_family))
    if x or x2: plt.legend()
    else: annotate_empty("No fill equity paths")
    plt.title("Baseline vs enriched equity");plt.xlabel("decision ns");plt.ylabel("PnL")
    save(root,"03_baseline_vs_enriched_equity.png")

    heatmap(root,"04_baseline_entry_exit_heatmap.png","Baseline entry × exit PnL",baseline["metrics"])
    heatmap(root,"05_rich_entry_exit_heatmap.png","Rich-model entry × exit PnL",(rich or {}).get("metrics",{}))
    heatmap(root,"06_incremental_heatmap.png","Rich minus baseline ΔPnL",delta,delta=True)

    plt.figure(figsize=(9,4.5))
    drawn=False
    for family,curve in sorted(info.items()):
        xs=[];ys=[]
        for delay in DELAYS:
            v=curve.get(str(delay))
            if isinstance(v,(int,float)):
                xs.append(delay);ys.append(float(v))
        if xs:
            plt.plot(xs,ys,marker="o",label=family);drawn=True
    if drawn: plt.legend(fontsize=7,ncol=2)
    else: annotate_empty("No delay curve support")
    plt.title("Information-latency frontier");plt.xlabel("artificial information delay ms");plt.ylabel("LOCAL_TEST PnL")
    save(root,"07_information_latency_frontier.png")

    plt.figure(figsize=(9,4.5))
    drawn=False
    for family,curve in sorted(signal.items()):
        xs=[];ys=[]
        for horizon in EXITS:
            v=curve.get(str(horizon))
            if isinstance(v,(int,float)):
                xs.append(horizon);ys.append(float(v))
        if xs:
            plt.plot(xs,ys,marker="o",label=family);drawn=True
    if drawn: plt.legend(fontsize=7,ncol=2)
    else: annotate_empty("No signal-decay support")
    plt.title("Signal decay");plt.xlabel("exit horizon ms");plt.ylabel("mean PnL across entry latencies")
    save(root,"08_signal_decay.png")

    plt.figure(figsize=(8,5))
    if actions:
        families=sorted(actions)
        yes=[actions[f].get("YES",0) for f in families]
        no=[actions[f].get("NO",0) for f in families]
        plt.scatter(yes,no)
        for f,xv,yv in zip(families,yes,no): plt.annotate(f,(xv,yv),fontsize=7)
        plt.xlabel("YES actions");plt.ylabel("NO actions")
    else: annotate_empty("No action map support")
    plt.title("Momentum / reversal action map")
    save(root,"09_momentum_reversal_map.png")

    plt.figure(figsize=(10,4.5))
    families=[r.get("family") for r in scorecard]
    values=[float(r.get("median_delta_pnl") or 0.0) for r in scorecard]
    if families:
        plt.bar(range(len(families)),values)
        plt.xticks(range(len(families)),families,rotation=70,ha="right",fontsize=8)
    else: annotate_empty("No family scorecard")
    plt.ylabel("median ΔPnL across cells");plt.title("PnL contribution by alpha family")
    save(root,"10_alpha_family_contribution.png")

    plt.figure(figsize=(8,4.5))
    decisions=((dynamic.get("diagnostic") or {}).get("decisions") or {})
    if decisions:
        labels=list(decisions);vals=[decisions[x] for x in labels]
        plt.bar(range(len(labels)),vals);plt.xticks(range(len(labels)),labels,rotation=45,ha="right")
    else: annotate_empty("Dynamic-exit evidence insufficient")
    plt.title("Dynamic exit vs fixed-exit research decisions")
    save(root,"11_dynamic_exit.png")

    for field,title,name in (
        ("asset","PnL by asset","12_pnl_by_asset.png"),
        ("contract_horizon","PnL by contract horizon","13_pnl_by_contract_horizon.png"),
    ):
        agg=defaultdict(float)
        for row in r_events:
            agg[str(row.get(field) or "UNKNOWN")]+=float(row["cash_pnl"])
        plt.figure(figsize=(9,4.5))
        labels=sorted(agg)
        if labels:
            plt.bar(range(len(labels)),[agg[x] for x in labels])
            plt.xticks(range(len(labels)),labels,rotation=45,ha="right")
        else: annotate_empty("No enriched fill attribution")
        plt.ylabel("PnL");plt.title(title)
        save(root,name)

    # Exhaustive equity gallery: every model, every latency group, every exit group.
    gallery=root/"equity_gallery"
    gallery.mkdir(parents=True,exist_ok=True)
    sources={"BASELINE":baseline.get("equity_events") or {}}
    for family,value in sorted(nested.items()):
        local=(value or {}).get("local_test") or {}
        events=local.get("events") or {}
        if events:
            sources[family]=events

    gallery_files=[]
    grid_rows=[]
    for model_name,event_map in sources.items():
        model_safe=safe_name(model_name)
        model_dir=gallery/model_safe
        model_dir.mkdir(parents=True,exist_ok=True)

        # One chart with all 60 cells.
        name=f"equity_gallery/{model_safe}/00_all_60_cells.png"
        plot_equity_group(
            root,event_map,
            [f"{l}::{h}" for l in LATENCIES for h in EXITS],
            f"{model_name}: all entry × exit equity lines",
            name,
        )
        gallery_files.append(name)

        # Six charts: each entry latency with all ten exits.
        for latency in LATENCIES:
            name=f"equity_gallery/{model_safe}/latency_{latency}ms_all_exits.png"
            plot_equity_group(
                root,event_map,
                [f"{latency}::{h}" for h in EXITS],
                f"{model_name}: entry {latency}ms, all exits",
                name,
            )
            gallery_files.append(name)

        # Ten charts: each exit horizon with all six entry latencies.
        for horizon in EXITS:
            name=f"equity_gallery/{model_safe}/exit_{horizon}ms_all_latencies.png"
            plot_equity_group(
                root,event_map,
                [f"{l}::{horizon}" for l in LATENCIES],
                f"{model_name}: exit {horizon}ms, all entry latencies",
                name,
            )
            gallery_files.append(name)

        metrics = baseline.get("metrics") if model_name=="BASELINE" else (
            ((nested.get(model_name) or {}).get("local_test") or {}).get("metrics") or {}
        )
        deltas = {} if model_name=="BASELINE" else (
            (nested.get(model_name) or {}).get("delta_vs_exact_baseline_local_test") or {}
        )
        for latency in LATENCIES:
            for horizon in EXITS:
                key=f"{latency}::{horizon}"
                cell=metrics.get(key) or {}
                grid_rows.append({
                    "model":model_name,
                    "entry_latency_ms":latency,
                    "exit_horizon_ms":horizon,
                    "total_pnl":cell.get("total_pnl"),
                    "fills":cell.get("fills"),
                    "trade_count":cell.get("trade_count",cell.get("observed_actions")),
                    "pnl_per_trade":cell.get("pnl_per_trade"),
                    "pnl_per_fill":cell.get("pnl_per_fill"),
                    "hit_rate":cell.get("hit_rate"),
                    "max_drawdown":cell.get("max_drawdown"),
                    "delta_vs_baseline":deltas.get(key),
                })

    with (gallery/"pnl_grid_all_models.csv").open("w",newline="",encoding="utf-8") as handle:
        fields=[
            "model","entry_latency_ms","exit_horizon_ms","total_pnl","fills",
            "trade_count","pnl_per_trade","pnl_per_fill","hit_rate","max_drawdown",
            "delta_vs_baseline",
        ]
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        writer.writerows(grid_rows)

    (gallery/"gallery_manifest.json").write_text(json.dumps({
        "schema":"polymarket_v7_multi_alpha_2h_equity_gallery_v1",
        "models":sorted(sources),
        "latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),
        "cells_per_model":len(LATENCIES)*len(EXITS),
        "grouped_equity_figures_per_model":1+len(LATENCIES)+len(EXITS),
        "figure_count":len(gallery_files),
        "figures":gallery_files,
        "grid_csv":"equity_gallery/pnl_grid_all_models.csv",
    },indent=2,sort_keys=True)+"\n",encoding="utf-8")

    manifest={
        "schema":"polymarket_v7_multi_alpha_2h_figures_v1",
        "best_enriched_family":rich_family,"baseline_cell":bkey,"rich_cell":rkey,
        "figures":[
            "01_baseline_equity.png","02_best_enriched_equity.png","03_baseline_vs_enriched_equity.png",
            "04_baseline_entry_exit_heatmap.png","05_rich_entry_exit_heatmap.png","06_incremental_heatmap.png",
            "07_information_latency_frontier.png","08_signal_decay.png","09_momentum_reversal_map.png",
            "10_alpha_family_contribution.png","11_dynamic_exit.png","12_pnl_by_asset.png","13_pnl_by_contract_horizon.png",
        ],
        "equity_gallery":"equity_gallery/gallery_manifest.json",
        "pnl_grid_csv":"equity_gallery/pnl_grid_all_models.csv",
    }
    (root/"figures_manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
