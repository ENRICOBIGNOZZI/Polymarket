"""Large deterministic alpha/equity library on frozen 2H native causal evidence.

Research-only. No model selection, deployment, authentication or order authority.
Every alpha uses only information available at the decision row. Execution uses
native kind=6 causal repricing evidence; missing evidence is censored.
"""
from __future__ import annotations

import argparse
import csv
import gzip
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, finite
from research.walk_forward_v3.direct_action import (
    _valid_state, decision_side_state, selected_action_side,
)
from research.walk_forward_v3.multi_alpha_2h import (
    LATENCIES, EXITS, WINDOW_NS, candidate_windows, chronological_split,
    equity_stats, native_execute_side_cell, row_features,
)

SCHEMA="polymarket_v7_native_2h_alpha_library_v1"


def sign(value):
    if not finite(value) or abs(float(value)) <= 1e-15:
        return 0
    return 1 if float(value) > 0 else -1


def opposite(side):
    return "NO" if str(side)=="YES" else "YES"


def side_for_direction(row, desired):
    base=selected_action_side(row)
    direction=sign(row.get("direction"))
    if desired==0 or direction==0:
        return None
    return base if desired==direction else opposite(base)


def first_feature(row,*names):
    f=row.get("features") or {}
    for name in names:
        value=f.get(name)
        if finite(value):
            return float(value)
    return None


def venue_returns(row):
    f=row.get("features") or {}
    vals=[]
    for names in (
        ("external.binance_return_100ms_bp","binance_return_100ms_bp"),
        ("external.coinbase_return_100ms_bp","coinbase_return_100ms_bp"),
        ("external.bybit_return_100ms_bp","bybit_return_100ms_bp"),
    ):
        value=None
        for name in names:
            if finite(f.get(name)):
                value=float(f[name]);break
        if value is not None:
            vals.append(value)
    return vals


def feature(row,name):
    aliases={
        "ret250":("external.return_250ms","return_250ms"),
        "ret1s":("external.return_1s","return_1s"),
        "ret5s":("external.return_5s","return_5s"),
        "dispersion":("external.dispersion_bps","dispersion_bps"),
        "vol_fast":("external.native_vol_fast","native_vol_fast"),
        "vol_slow":("external.native_vol_slow","native_vol_slow"),
        "signal":("signal_return_bp","external.binance_return_100ms_bp","binance_return_100ms_bp"),
    }
    return first_feature(row,*aliases[name])


def quantile(values,q):
    values=sorted(float(v) for v in values if finite(v))
    if not values:
        return None
    i=max(0,min(len(values)-1,int(math.ceil(q*len(values)))-1))
    return values[i]


def native_support(row):
    arrivals=row.get("arrivals") or {}
    targets=row.get("targets") or {}
    entry=sum(
        isinstance(arrivals.get(str(l)),dict)
        for l in LATENCIES
    )
    exits=sum(
        isinstance(targets.get(str(h)),dict)
        and targets[str(h)].get("state")=="OBSERVED"
        for h in EXITS
    )
    return entry,exits


def select_window(rows):
    candidates=candidate_windows(rows)
    if not candidates:
        raise ValueError("NO_TWO_HOUR_CANDIDATE")
    best=None
    for c in candidates:
        selected=c["rows"]
        supports=[native_support(r) for r in selected]
        entry=sum(x for x,_ in supports)
        exits=sum(y for _,y in supports)
        full=sum(x==len(LATENCIES) and y==len(EXITS) for x,y in supports)
        score=(
            c["assets"],c["contracts"],full,
            entry+exits,c["markets"],c["decisions"],-c["start_ns"],
        )
        c["native_full_rows"]=full
        c["native_entry_cells"]=entry
        c["native_exit_cells"]=exits
        c["selection_score"]=list(score)
        if best is None or score>tuple(best["selection_score"]):
            best=c
    out=dict(best)
    window_rows=out.pop("rows")
    out["selection_policy"]="DATA_QUALITY_AND_NATIVE_CAUSAL_COVERAGE_ONLY_NO_PNL"
    out["profitability_used_for_selection"]=False
    return out,window_rows


def training_thresholds(rows):
    data=defaultdict(list)
    for row in rows:
        s=feature(row,"signal")
        if finite(s): data["abs_signal"].append(abs(s))
        for name in ("dispersion","vol_fast"):
            v=feature(row,name)
            if finite(v): data[name].append(v)
        vf,vs=feature(row,"vol_fast"),feature(row,"vol_slow")
        if finite(vf) and finite(vs) and abs(vs)>1e-12:
            data["vol_ratio"].append(vf/abs(vs))
    return {
        "abs_signal_median":quantile(data["abs_signal"],.50),
        "abs_signal_q75":quantile(data["abs_signal"],.75),
        "dispersion_median":quantile(data["dispersion"],.50),
        "vol_fast_median":quantile(data["vol_fast"],.50),
        "vol_ratio_median":quantile(data["vol_ratio"],.50),
    }


def pm_yes_imbalance(row):
    pair=row.get("pair") if isinstance(row.get("pair"),dict) else {}
    yes=pair.get("yes") if isinstance(pair.get("yes"),dict) else {}
    bd=yes.get("bid_quantity");ad=yes.get("ask_quantity")
    if not finite(bd) or not finite(ad) or float(bd)+float(ad)<=0:
        return None
    return (float(bd)-float(ad))/(float(bd)+float(ad))


def pm_yes_mid(row):
    pair=row.get("pair") if isinstance(row.get("pair"),dict) else {}
    yes=pair.get("yes") if isinstance(pair.get("yes"),dict) else {}
    bid,ask=yes.get("bid"),yes.get("ask")
    if finite(bid) and finite(ask):
        return (float(bid)+float(ask))/2
    return None


def alpha_library(th):
    def baseline(row): return selected_action_side(row)
    def reversal(row): return opposite(selected_action_side(row))

    def age_cap(ms):
        return lambda row: selected_action_side(row) if float(row.get("signal_age_ns") or 1e30)/1e6<=ms else None

    def strength(which,keep_high=True):
        threshold=th.get(which)
        def rule(row):
            v=feature(row,"signal")
            if threshold is None or not finite(v): return None
            ok=abs(v)>=threshold if keep_high else abs(v)<=threshold
            return selected_action_side(row) if ok else None
        return rule

    def ret_rule(name,reverse=False):
        def rule(row):
            v=feature(row,name)
            s=sign(v)
            return side_for_direction(row,-s if reverse else s)
        return rule

    def consensus(row,reverse=False,require_all=False):
        vals=venue_returns(row)
        if not vals:return None
        signs=[sign(v) for v in vals if sign(v)]
        if not signs:return None
        if require_all and (len(signs)<2 or len(set(signs))!=1):return None
        s=sign(sum(vals))
        return side_for_direction(row,-s if reverse else s)

    def dispersion(low=True,reverse=False):
        threshold=th.get("dispersion_median")
        def rule(row):
            d=feature(row,"dispersion")
            if threshold is None or not finite(d):return None
            if (float(d)<=threshold) != low:return None
            return consensus(row,reverse=reverse)
        return rule

    def vol_regime(high,reverse=False):
        threshold=th.get("vol_fast_median")
        def rule(row):
            v=feature(row,"vol_fast")
            if threshold is None or not finite(v):return None
            if (float(v)>=threshold)!=high:return None
            return consensus(row,reverse=reverse)
        return rule

    def acceleration(row,reverse=False):
        r250,r1=feature(row,"ret250"),feature(row,"ret1s")
        if not finite(r250) or not finite(r1):return None
        a=4.0*float(r250)-float(r1)
        return side_for_direction(row,-sign(a) if reverse else sign(a))

    def deceleration_reversal(row):
        r1,r5=feature(row,"ret1s"),feature(row,"ret5s")
        if not finite(r1) or not finite(r5):return None
        d=float(r1)-float(r5)/5.0
        return side_for_direction(row,-sign(d))

    def pm_imbalance(row,reverse=False):
        x=pm_yes_imbalance(row)
        if not finite(x) or abs(float(x))<1e-9:return None
        side="YES" if float(x)>0 else "NO"
        return opposite(side) if reverse else side

    def pm_extension_reversal(row):
        mid=pm_yes_mid(row)
        if not finite(mid) or abs(float(mid)-.5)<1e-9:return None
        return "NO" if float(mid)>.5 else "YES"

    return {
        "baseline_continuation":baseline,
        "baseline_reversal":reversal,
        "signal_age_le_10ms":age_cap(10),
        "signal_age_le_25ms":age_cap(25),
        "signal_age_le_50ms":age_cap(50),
        "signal_age_le_100ms":age_cap(100),
        "strong_signal_q75":strength("abs_signal_q75",True),
        "weak_signal_bottom50":strength("abs_signal_median",False),
        "ret250_momentum":ret_rule("ret250",False),
        "ret250_reversal":ret_rule("ret250",True),
        "ret1s_momentum":ret_rule("ret1s",False),
        "ret1s_reversal":ret_rule("ret1s",True),
        "ret5s_momentum":ret_rule("ret5s",False),
        "ret5s_reversal":ret_rule("ret5s",True),
        "cross_venue_consensus_momentum":lambda r:consensus(r,False,False),
        "cross_venue_consensus_reversal":lambda r:consensus(r,True,False),
        "cross_venue_full_agreement_momentum":lambda r:consensus(r,False,True),
        "cross_venue_full_agreement_reversal":lambda r:consensus(r,True,True),
        "low_dispersion_momentum":dispersion(True,False),
        "low_dispersion_reversal":dispersion(True,True),
        "high_dispersion_momentum":dispersion(False,False),
        "high_dispersion_reversal":dispersion(False,True),
        "low_vol_consensus_momentum":vol_regime(False,False),
        "low_vol_consensus_reversal":vol_regime(False,True),
        "high_vol_consensus_momentum":vol_regime(True,False),
        "high_vol_consensus_reversal":vol_regime(True,True),
        "acceleration_momentum":lambda r:acceleration(r,False),
        "acceleration_reversal":lambda r:acceleration(r,True),
        "deceleration_reversal":deceleration_reversal,
        "pm_yes_imbalance":lambda r:pm_imbalance(r,False),
        "pm_yes_imbalance_reversal":lambda r:pm_imbalance(r,True),
        "pm_extension_reversal":pm_extension_reversal,
    }


def evaluate_alpha(rows,rule):
    cells={};events={}
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            selected=observed=fills=0
            censored=Counter();seq=[]
            for row in rows:
                side=rule(row)
                if side is None:
                    continue
                selected+=1
                economics,state=native_execute_side_cell(row,latency,horizon,side)
                if economics is None:
                    censored[state]+=1
                    continue
                observed+=1
                if float(economics.get("filled") or 0)>0:
                    fills+=1
                    seq.append({
                        "decision_ns":int(row["decision_ns"]),
                        "decision_id":str(row["decision_id"]),
                        "market_id":str(row["market_id"]),
                        "asset":str(row.get("asset") or "UNKNOWN"),
                        "contract_horizon":str(row.get("horizon") or "UNKNOWN"),
                        "side":side,
                        "cash_pnl":float(economics["cash_pnl"]),
                    })
            stats=equity_stats(seq)
            cells[key]={
                "opportunities":len(rows),"selected":selected,"observed":observed,
                "fills":fills,"censored":sum(censored.values()),
                "censoring_reasons":dict(censored),
                "total_pnl":stats["total_pnl"] if observed else None,
                "pnl_per_selected":stats["total_pnl"]/selected if selected else None,
                "pnl_per_observed":stats["total_pnl"]/observed if observed else None,
                "pnl_per_fill":stats["pnl_per_fill"],"hit_rate":stats["hit_rate"],
                "max_drawdown":stats["max_drawdown"],
            }
            events[key]=sorted(seq,key=lambda x:(x["decision_ns"],x["decision_id"]))
    return {"cells":cells,"events":events}



def cumulative(events):
    total=0.0;xs=[];ys=[]
    seq=sorted(events,key=lambda x:(x["decision_ns"],x["decision_id"]))
    if not seq:return xs,ys
    origin=int(seq[0]["decision_ns"])
    for row in seq:
        total+=float(row["cash_pnl"])
        xs.append((int(row["decision_ns"])-origin)/60_000_000_000)
        ys.append(total)
    return xs,ys


def plot_group(path,event_map,keys,title,legend=True):
    path.parent.mkdir(parents=True,exist_ok=True)
    plt.figure(figsize=(10,5))
    drawn=False
    for key in keys:
        x,y=cumulative(event_map.get(key,[]))
        if not x:continue
        plt.plot(x,y,linewidth=1.0,label=key)
        drawn=True
    if drawn:
        plt.axhline(0,linewidth=.7)
        if legend:plt.legend(fontsize=6,ncol=2)
        plt.xlabel("minutes since first fill")
        plt.ylabel("cumulative PnL")
    else:
        plt.text(.5,.5,"No observed fills",ha="center",va="center",transform=plt.gca().transAxes)
        plt.xticks([]);plt.yticks([])
    plt.title(title)
    plt.tight_layout()
    plt.savefig(path,dpi=105,bbox_inches="tight")
    plt.close()


def write_equity_outputs(output,results):
    gallery=output/"native_alpha_equity_gallery"
    gallery.mkdir(parents=True,exist_ok=True)
    files=[]
    with gzip.open(output/"22_native_alpha_equity_paths.csv.gz","wt",newline="",encoding="utf-8") as handle:
        fields=["alpha","cell","decision_ns","cash_pnl","cumulative_pnl","asset","contract_horizon","side"]
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for name,res in sorted(results.items()):
            safe="".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
            event_map=res["events"]
            all_name=gallery/safe/"00_all_60_cells.png"
            plot_group(
                all_name,event_map,
                [f"{l}::{h}" for l in LATENCIES for h in EXITS],
                f"{name}: all 60 entry × exit equity lines",legend=False)
            files.append(str(all_name.relative_to(output)))
            for latency in LATENCIES:
                path=gallery/safe/f"latency_{latency}ms_all_exits.png"
                plot_group(
                    path,event_map,[f"{latency}::{h}" for h in EXITS],
                    f"{name}: entry {latency}ms, all exits",legend=True)
                files.append(str(path.relative_to(output)))
            for cell,events in sorted(event_map.items()):
                total=0.0
                for row in sorted(events,key=lambda x:(x["decision_ns"],x["decision_id"])):
                    total+=float(row["cash_pnl"])
                    writer.writerow({
                        "alpha":name,"cell":cell,"decision_ns":row["decision_ns"],
                        "cash_pnl":row["cash_pnl"],"cumulative_pnl":total,
                        "asset":row["asset"],"contract_horizon":row["contract_horizon"],
                        "side":row["side"],
                    })
    manifest={
        "schema":"polymarket_v7_native_2h_alpha_equity_gallery_v1",
        "alpha_count":len(results),
        "cells_per_alpha":len(LATENCIES)*len(EXITS),
        "figures_per_alpha":1+len(LATENCIES),
        "figure_count":len(files),
        "figures":files,
        "raw_equity_paths":"22_native_alpha_equity_paths.csv.gz",
        "grid_csv":"21_native_alpha_grid.csv",
    }
    atomic_json(gallery/"gallery_manifest.json",manifest)
    return manifest


def run(root,output,minimum_wall_ns):
    output.mkdir(parents=True,exist_ok=True)
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":raise ValueError("DATA_NOT_READY")
    rows=[r for r in data["decisions"] if _valid_state(r)]
    window,window_rows=select_window(rows)
    splits=chronological_split(window_rows)
    th=training_thresholds(splits["TRAIN"])
    alphas=alpha_library(th)
    results={}
    for name,rule in alphas.items():
        results[name]=evaluate_alpha(window_rows,rule)

    gallery=write_equity_outputs(output,results)
    payload={
        "schema":SCHEMA,**SAFETY,"research_only":True,"automatic_promotion":False,
        "window":window,"thresholds_fit_on_first_60pct":th,
        "latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),
        "alphas":{name:{"cells":value["cells"]} for name,value in results.items()},
        "equity_gallery":gallery,
    }
    atomic_json(output/"20_native_alpha_library.json",payload)

    with (output/"21_native_alpha_grid.csv").open("w",newline="",encoding="utf-8") as handle:
        fields=["alpha","entry_latency_ms","exit_horizon_ms","total_pnl","selected","observed","fills","pnl_per_selected","pnl_per_observed","pnl_per_fill","hit_rate","max_drawdown"]
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for name,res in results.items():
            for latency in LATENCIES:
                for horizon in EXITS:
                    cell=res["cells"][f"{latency}::{horizon}"]
                    writer.writerow({"alpha":name,"entry_latency_ms":latency,"exit_horizon_ms":horizon,**{k:cell.get(k) for k in fields[3:]}})
    return payload


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    out=run(a.root,a.output_dir,a.minimum_wall_ns)
    print(json.dumps({"state":"READY","alphas":len(out["alphas"]),"window":out["window"]},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
