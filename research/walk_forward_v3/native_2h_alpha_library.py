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
from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, finite
from research.walk_forward_v3.direct_action import (
    _valid_state, decision_side_state, selected_action_side,
    realized_action_economics,
)

SCHEMA="polymarket_v7_native_2h_alpha_library_v1"
LATENCIES=(5,10,25,50,100,250)
EXITS=(500,750,1000,1500,2000,3000,4000,5000,7500,10000)
SIZE=5.0
WINDOW_NS=2*60*60*1_000_000_000
STEP_NS=15*60*1_000_000_000


def candidate_windows(rows):
    times=sorted(int(r["decision_ns"]) for r in rows)
    if not times or times[-1]-times[0]<WINDOW_NS:
        return []
    first=(times[0]//STEP_NS)*STEP_NS
    ordered=sorted(rows,key=lambda r:(int(r["decision_ns"]),str(r["decision_id"])))
    output=[]
    left=right=0
    n=len(ordered)
    for start in range(first,times[-1]-WINDOW_NS+1,STEP_NS):
        end=start+WINDOW_NS
        while left<n and int(ordered[left]["decision_ns"])<start:left+=1
        if right<left:right=left
        while right<n and int(ordered[right]["decision_ns"])<end:right+=1
        subset=ordered[left:right]
        if subset:
            output.append({
                "start_ns":start,"end_ns":end,"rows":subset,
                "assets":len({str(r.get("asset") or "UNKNOWN") for r in subset}),
                "contracts":len({str(r.get("horizon") or "UNKNOWN") for r in subset}),
                "markets":len({str(r["market_id"]) for r in subset}),
                "decisions":len(subset),
            })
    return output


def chronological_split(rows):
    ordered=sorted(rows,key=lambda r:(int(r["decision_ns"]),str(r["decision_id"])))
    n=len(ordered)
    a,b=max(1,int(n*.60)),max(2,int(n*.80))
    b=min(max(a+1,b),n)
    return {"TRAIN":ordered[:a],"VALIDATION":ordered[a:b],"LOCAL_TEST":ordered[b:]}


def equity_stats(events):
    equity=peak=max_dd=0.0
    positive=negative=zero=0
    for event in sorted(events,key=lambda e:(e["decision_ns"],e["decision_id"])):
        pnl=float(event["cash_pnl"])
        equity+=pnl
        peak=max(peak,equity)
        max_dd=max(max_dd,peak-equity)
        positive+=pnl>1e-15
        negative+=pnl<-1e-15
        zero+=abs(pnl)<=1e-15
    fills=len(events)
    return {
        "total_pnl":equity,"fills":fills,
        "pnl_per_fill":equity/fills if fills else None,
        "hit_rate":positive/(positive+negative) if positive+negative else None,
        "max_drawdown":max_dd,"positive":positive,"negative":negative,"zero":zero,
    }


def native_execute_side_cell(row,latency_ms,exit_ms,side):
    return realized_action_economics(
        row,size=SIZE,horizon_ms=int(exit_ms),latency_ms=int(latency_ms),
        side=side,entry_cap=.99,hard_order_notional=100.0,
        require_full_decision_depth=True,
    )



def sign(value):
    if not finite(value) or abs(float(value)) <= 1e-15:
        return 0
    return 1 if float(value) > 0 else -1


def opposite(side):
    side=str(side)
    if side=="YES": return "NO"
    if side=="NO": return "YES"
    return None


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
        "binance100":("external.binance_return_100ms_bp","binance_return_100ms_bp"),
        "coinbase100":("external.coinbase_return_100ms_bp","coinbase_return_100ms_bp"),
        "fresh_venues":("external.fresh_venues","fresh_venues"),
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
        sig=feature(row,"signal")
        if finite(sig): data["abs_signal"].append(abs(sig))
        for name in ("dispersion","vol_fast","fresh_venues"):
            v=feature(row,name)
            if finite(v): data[name].append(v)
        vf,vs=feature(row,"vol_fast"),feature(row,"vol_slow")
        if finite(vf) and finite(vs) and abs(vs)>1e-12:
            data["vol_ratio"].append(vf/abs(vs))
        if finite(row.get("tte_ns")):
            data["tte_s"].append(float(row["tte_ns"])/1e9)
        side=selected_action_side(row)
        state=decision_side_state(row,side)
        if state is not None:
            bid,ask=float(state["bid"]),float(state["ask"])
            depth=float(state["ask_quantity"])
            data["selected_spread"].append(ask-bid)
            data["selected_depth"].append(depth)
            data["selected_ask"].append(ask)
    return {
        "abs_signal_median":quantile(data["abs_signal"],.50),
        "abs_signal_q75":quantile(data["abs_signal"],.75),
        "abs_signal_q90":quantile(data["abs_signal"],.90),
        "dispersion_median":quantile(data["dispersion"],.50),
        "dispersion_q75":quantile(data["dispersion"],.75),
        "vol_fast_median":quantile(data["vol_fast"],.50),
        "vol_ratio_median":quantile(data["vol_ratio"],.50),
        "fresh_venues_median":quantile(data["fresh_venues"],.50),
        "tte_s_median":quantile(data["tte_s"],.50),
        "selected_spread_median":quantile(data["selected_spread"],.50),
        "selected_depth_median":quantile(data["selected_depth"],.50),
        "selected_ask_median":quantile(data["selected_ask"],.50),
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


def selected_state_value(row,key):
    state=decision_side_state(row,selected_action_side(row))
    if state is None:return None
    if key=="spread":
        return float(state["ask"])-float(state["bid"])
    if key=="depth":
        return float(state["ask_quantity"])
    if key=="ask":
        return float(state["ask"])
    return None


def baseline_if_threshold(row,value,threshold,high=True):
    if threshold is None or not finite(value):return None
    if (float(value)>=float(threshold))!=high:return None
    return selected_action_side(row)


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

    def venue_rule(name,reverse=False):
        def rule(row):
            v=feature(row,name)
            return side_for_direction(row,-sign(v) if reverse else sign(v))
        return rule

    def agreement_with_baseline(row,reverse=False):
        vals=venue_returns(row)
        sig=feature(row,"signal")
        if not vals or not finite(sig):return None
        c=sign(sum(vals));s=sign(sig)
        if c==0 or s==0 or c!=s:return None
        return opposite(selected_action_side(row)) if reverse else selected_action_side(row)

    def disagreement_trade(row,follow_external=True):
        vals=venue_returns(row)
        sig=feature(row,"signal")
        if not vals or not finite(sig):return None
        c=sign(sum(vals));s=sign(sig)
        if c==0 or s==0 or c==s:return None
        desired=c if follow_external else s
        return side_for_direction(row,desired)

    def fresh_confirmation(row,min_venues,reverse=False):
        fresh=feature(row,"fresh_venues")
        if not finite(fresh) or float(fresh)<min_venues:return None
        return consensus(row,reverse=reverse)

    def vol_ratio_regime(row,high=True,reverse=False):
        vf,vs=feature(row,"vol_fast"),feature(row,"vol_slow")
        threshold=th.get("vol_ratio_median")
        if not finite(vf) or not finite(vs) or abs(float(vs))<=1e-12 or threshold is None:return None
        ratio=float(vf)/abs(float(vs))
        if (ratio>=threshold)!=high:return None
        return consensus(row,reverse=reverse)

    def regime_threshold(key,threshold_key,high=True,reverse=False):
        threshold=th.get(threshold_key)
        def rule(row):
            value=(
                float(row.get("tte_ns") or 0)/1e9 if key=="tte_s"
                else selected_state_value(row,key)
            )
            side=baseline_if_threshold(row,value,threshold,high)
            if side is None:return None
            return opposite(side) if reverse else side
        return rule

    def dispersion_extreme(row,high=True,reverse=False):
        threshold=th.get("dispersion_q75")
        d=feature(row,"dispersion")
        if threshold is None or not finite(d):return None
        if (float(d)>=threshold)!=high:return None
        return consensus(row,reverse=reverse)

    def strong_signal_vol(row,high_vol=True,reverse=False):
        sig=feature(row,"signal");vol=feature(row,"vol_fast")
        st=th.get("abs_signal_q75");vt=th.get("vol_fast_median")
        if st is None or vt is None or not finite(sig) or not finite(vol):return None
        if abs(float(sig))<st or ((float(vol)>=vt)!=high_vol):return None
        return opposite(selected_action_side(row)) if reverse else selected_action_side(row)

    return {
        "baseline_continuation":baseline,
        "baseline_reversal":reversal,
        "signal_age_le_10ms":age_cap(10),
        "signal_age_le_25ms":age_cap(25),
        "signal_age_le_50ms":age_cap(50),
        "signal_age_le_100ms":age_cap(100),
        "strong_signal_q75":strength("abs_signal_q75",True),
        "very_strong_signal_q90":strength("abs_signal_q90",True),
        "weak_signal_bottom50":strength("abs_signal_median",False),
        "ret250_momentum":ret_rule("ret250",False),
        "ret250_reversal":ret_rule("ret250",True),
        "ret1s_momentum":ret_rule("ret1s",False),
        "ret1s_reversal":ret_rule("ret1s",True),
        "ret5s_momentum":ret_rule("ret5s",False),
        "ret5s_reversal":ret_rule("ret5s",True),
        "binance100_momentum":venue_rule("binance100",False),
        "binance100_reversal":venue_rule("binance100",True),
        "coinbase100_momentum":venue_rule("coinbase100",False),
        "coinbase100_reversal":venue_rule("coinbase100",True),
        "cross_venue_consensus_momentum":lambda r:consensus(r,False,False),
        "cross_venue_consensus_reversal":lambda r:consensus(r,True,False),
        "cross_venue_full_agreement_momentum":lambda r:consensus(r,False,True),
        "cross_venue_full_agreement_reversal":lambda r:consensus(r,True,True),
        "signal_crossvenue_agreement":lambda r:agreement_with_baseline(r,False),
        "signal_crossvenue_agreement_reversal":lambda r:agreement_with_baseline(r,True),
        "signal_crossvenue_disagree_follow_external":lambda r:disagreement_trade(r,True),
        "signal_crossvenue_disagree_follow_signal":lambda r:disagreement_trade(r,False),
        "fresh_venues_ge_2_momentum":lambda r:fresh_confirmation(r,2,False),
        "fresh_venues_ge_2_reversal":lambda r:fresh_confirmation(r,2,True),
        "fresh_venues_ge_3_momentum":lambda r:fresh_confirmation(r,3,False),
        "fresh_venues_ge_3_reversal":lambda r:fresh_confirmation(r,3,True),
        "low_dispersion_momentum":dispersion(True,False),
        "low_dispersion_reversal":dispersion(True,True),
        "high_dispersion_momentum":dispersion(False,False),
        "high_dispersion_reversal":dispersion(False,True),
        "top_quartile_dispersion_momentum":lambda r:dispersion_extreme(r,True,False),
        "top_quartile_dispersion_reversal":lambda r:dispersion_extreme(r,True,True),
        "low_vol_consensus_momentum":vol_regime(False,False),
        "low_vol_consensus_reversal":vol_regime(False,True),
        "high_vol_consensus_momentum":vol_regime(True,False),
        "high_vol_consensus_reversal":vol_regime(True,True),
        "high_vol_ratio_momentum":lambda r:vol_ratio_regime(r,True,False),
        "high_vol_ratio_reversal":lambda r:vol_ratio_regime(r,True,True),
        "low_vol_ratio_momentum":lambda r:vol_ratio_regime(r,False,False),
        "low_vol_ratio_reversal":lambda r:vol_ratio_regime(r,False,True),
        "strong_signal_high_vol":lambda r:strong_signal_vol(r,True,False),
        "strong_signal_high_vol_reversal":lambda r:strong_signal_vol(r,True,True),
        "strong_signal_low_vol":lambda r:strong_signal_vol(r,False,False),
        "strong_signal_low_vol_reversal":lambda r:strong_signal_vol(r,False,True),
        "acceleration_momentum":lambda r:acceleration(r,False),
        "acceleration_reversal":lambda r:acceleration(r,True),
        "deceleration_reversal":deceleration_reversal,
        "pm_yes_imbalance":lambda r:pm_imbalance(r,False),
        "pm_yes_imbalance_reversal":lambda r:pm_imbalance(r,True),
        "tight_selected_spread":regime_threshold("spread","selected_spread_median",False,False),
        "wide_selected_spread":regime_threshold("spread","selected_spread_median",True,False),
        "deep_selected_book":regime_threshold("depth","selected_depth_median",True,False),
        "shallow_selected_book":regime_threshold("depth","selected_depth_median",False,False),
        "short_tte_continuation":regime_threshold("tte_s","tte_s_median",False,False),
        "long_tte_continuation":regime_threshold("tte_s","tte_s_median",True,False),
        "short_tte_reversal":regime_threshold("tte_s","tte_s_median",False,True),
        "long_tte_reversal":regime_threshold("tte_s","tte_s_median",True,True),
        "low_selected_price_continuation":regime_threshold("ask","selected_ask_median",False,False),
        "high_selected_price_continuation":regime_threshold("ask","selected_ask_median",True,False),
        "low_selected_price_reversal":regime_threshold("ask","selected_ask_median",False,True),
        "high_selected_price_reversal":regime_threshold("ask","selected_ask_median",True,True),
        "pm_extension_reversal":pm_extension_reversal,
    }


def evaluate_alpha(rows,rule,economics_cache):
    cells={};events={}
    selected_side={
        str(row["decision_id"]):rule(row)
        for row in rows
    }
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            selected=observed=fills=0
            censored=Counter();seq=[]
            for row in rows:
                side=selected_side[str(row["decision_id"])]
                if side is None:
                    continue
                selected+=1
                cache_key=(str(row["decision_id"]),str(side),int(latency),int(horizon))
                cached=economics_cache.get(cache_key)
                if cached is None:
                    economics,state=native_execute_side_cell(row,latency,horizon,side)
                    economics_cache[cache_key]=(economics,state)
                else:
                    economics,state=cached
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



def write_equity_outputs(output,results,window):
    gallery=output/"native_alpha_equity_gallery"
    gallery.mkdir(parents=True,exist_ok=True)
    start_ns=int(window["start_ns"])
    end_ns=int(window["end_ns"])
    if end_ns-start_ns!=WINDOW_NS:
        raise ValueError("WINDOW_NOT_EXACTLY_2H")
    sample_ns=60*1_000_000_000
    points=121
    path=output/"22_native_alpha_equity_1m.csv.gz"
    with gzip.open(path,"wt",newline="",encoding="utf-8") as handle:
        fields=["alpha","cell","minute","cumulative_pnl"]
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        for name,res in sorted(results.items()):
            for cell,events in sorted(res["events"].items()):
                seq=sorted(events,key=lambda x:(x["decision_ns"],x["decision_id"]))
                pos=0
                total=0.0
                for minute in range(points):
                    cutoff=start_ns+minute*sample_ns
                    while pos<len(seq) and int(seq[pos]["decision_ns"])<=cutoff:
                        total+=float(seq[pos]["cash_pnl"])
                        pos+=1
                    writer.writerow({
                        "alpha":name,"cell":cell,"minute":minute,
                        "cumulative_pnl":total,
                    })
    manifest={
        "schema":"polymarket_v7_native_2h_alpha_equity_gallery_v2",
        "alpha_count":len(results),
        "cells_per_alpha":len(LATENCIES)*len(EXITS),
        "generation_deferred":True,
        "sample_seconds":60,
        "points_per_cell":points,
        "equity_paths":"22_native_alpha_equity_1m.csv.gz",
        "grid_csv":"21_native_alpha_grid.csv",
    }
    atomic_json(gallery/"gallery_manifest.json",manifest)
    return manifest



def run(root,output,minimum_wall_ns,skip_figures=False):
    output.mkdir(parents=True,exist_ok=True)
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":raise ValueError("DATA_NOT_READY")
    rows=[r for r in data["decisions"] if _valid_state(r)]
    window,window_rows=select_window(rows)
    splits=chronological_split(window_rows)
    th=training_thresholds(splits["TRAIN"])
    alphas=alpha_library(th)
    results={}
    economics_cache={}
    for name,rule in alphas.items():
        results[name]=evaluate_alpha(window_rows,rule,economics_cache)

    gallery=write_equity_outputs(output,results,window)
    payload={
        "schema":SCHEMA,**SAFETY,"research_only":True,"automatic_promotion":False,
        "window":window,"thresholds_fit_on_first_60pct":th,
        "latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),
        "alphas":{name:{"cells":value["cells"]} for name,value in results.items()},
        "execution_cache_entries":len(economics_cache),
        "execution_cache_semantics":"EXACT_MEMOIZATION_ONLY_NO_ECONOMIC_CHANGE",
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
    p.add_argument("--skip-figures",action="store_true")
    a=p.parse_args(argv)
    out=run(a.root,a.output_dir,a.minimum_wall_ns,skip_figures=a.skip_figures)
    print(json.dumps({"state":"READY","alphas":len(out["alphas"]),"window":out["window"]},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
