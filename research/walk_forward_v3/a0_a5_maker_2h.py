"""Pre-registered A0-A5 alpha horse race on one causal 2H Polymarket window.

Stage-1 objective: learn when short-horizon PM repricing information exists.
The last 40% is locked OOS. No threshold, scaler, feature set or model is fit
on that OOS block. This module is research-only and has no execution authority.

A0: current decision baseline, no added alpha.
A1: external momentum / cross-venue only.
A2: Polymarket microstructure only.
A3: external + Polymarket microstructure.
A4: training-frozen PM-vs-external residual mean reversion.
A5: full execution alpha using every causally observed allowed family.

Maker JOIN/IMPROVE/TTL/queue is deliberately a later stage: this file first
identifies alpha horizon and latency using common executable economics.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import csv
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, feature_names, finite
from research.walk_forward_v3 import direct_action as da
from research.walk_forward_v3.dynamic_exit import DynamicExitValueModel, summarize_dynamic_exit
from research.walk_forward_v3.btc_compact_equity import (
    EXITS, LATENCIES, SIZE, jsonl_sessions, pair_asof_session, stream_sessions,
)
from research.walk_forward_v3 import multi_alpha_2h as m

SCHEMA="polymarket_v7_a0_a5_maker_alpha_horse_race_v1"
WINDOW_NS=2*60*60*1_000_000_000
POLICIES=(
    "A0_BASELINE_NO_ADDED_ALPHA",
    "A1_EXTERNAL_MOMENTUM",
    "A2_PM_MICROSTRUCTURE",
    "A3_EXTERNAL_PLUS_PM",
    "A4_RESIDUAL_MEAN_REVERSION",
    "A5_FULL_EXECUTION_ALPHA",
)
SAFETY_PLUS={**SAFETY,"automatic_promotion":False,"execution_authority":False,"research_only":True}

A1_FAMILIES=frozenset(("baseline","cross_venue"))
A2_FAMILIES=frozenset(("pm_response","ofi","flow"))
A3_FAMILIES=frozenset(("baseline","cross_venue","pm_response","ofi","flow"))
A5_FAMILIES=frozenset((
    "baseline","cross_venue","pm_response","ofi","flow","perp","oi_funding",
    "liquidations","volatility","cross_asset","options","settlement",
    "maker_queue","maker_inventory","maker_toxicity",
))


def _allowed_internal(name:str,families:frozenset[str])->bool:
    # Execution state common to all arms; not alpha.
    common=(
        "state.ask","state.bid","state.spread","state.depth","state.minimum",
        "state.tte_s","state.price_distance_from_half","action.side_sign",
        "action.size","action.size2","action.log_size","action.depth_fraction",
        "action.notional","action.notional_fraction_of_cap",
        "action.exit_horizon_ms","action.log_exit_horizon",
        "system.latency_ms","system.log_latency",
    )
    if name in common:
        return True
    if name.startswith(("asset::","contract::","asset_contract::","exit::","latency::")):
        return True
    if name.startswith("x."):
        family=m.classify_source_feature(name[2:])
        return family in families
    if name in ("state.signal_age_ms","state.direction","action.signal_alignment"):
        return "baseline" in families
    if name.startswith("age::") or "effective_action_age" in name:
        return "baseline" in families
    if name.startswith("interaction.") and "signal" in name:
        return "baseline" in families
    if name.startswith("state.cross_venue_") or name.startswith("state.return_") or name.startswith("state.trend_"):
        return "cross_venue" in families
    if name.startswith(("action.continuation_","action.reversal_")):
        return "cross_venue" in families
    if name in ("state.native_vol_fast","state.native_vol_slow","state.native_vol_ratio"):
        return "volatility" in families
    if name in ("state.external_dispersion_bps","state.fresh_venues"):
        return "cross_venue" in families
    if name=="action.price_extension":
        return "pm_response" in families
    if name.startswith(("asset_contract_signal::","asset_contract_age::")):
        return "baseline" in families
    if name.startswith("asset_contract_size::"):
        return True
    return False


class FixedFamilyModel(da.DirectActionValueModel):
    def __init__(self,*,families:Iterable[str],**kwargs):
        self.information_families=frozenset(families)
        super().__init__(**kwargs)

    def _base_feature_names(self,rows):
        available=feature_names(rows)
        selected=[]
        for name in available:
            family=m.classify_source_feature(name)
            if family in self.information_families:
                selected.append(name)
        return tuple(selected[:192])

    def _configure_levels(self,rows):
        super()._configure_levels(rows)
        self.model_feature_names=tuple(
            name for name in self.model_feature_names
            if _allowed_internal(name,self.information_families)
        )
        if not self.model_feature_names:
            raise ValueError("NO_FEATURES_FOR_FIXED_INFORMATION_SET")


def fit_model(rows,families):
    model=FixedFamilyModel(
        families=families,size_grid=(SIZE,),action_horizons_ms=EXITS,
        train_latencies_ms=LATENCIES,max_sizes_per_state=1,
        selection_calibration_mode="OFF",support_policy_mode="DIAGNOSTIC",
        conditional_calibration=False,streaming_batch_size=2048,ridge=8.0,
    )
    model.fit(rows)
    return model


def model_side(model,row,latency,horizon):
    values={}
    for side in da.decision_action_sides(row):
        state=da.decision_side_state(row,side)
        if state is None:
            continue
        if SIZE+1e-12<float(row["minimum"]):
            continue
        if SIZE>float(state["ask_quantity"])+1e-12:
            continue
        if SIZE*float(state["ask"])>da.DEFAULT_HARD_ORDER_NOTIONAL+1e-9:
            continue
        try:
            record=model._action_record(row,size=SIZE,horizon_ms=horizon,latency_ms=latency,side=side)
            value=float(model.mean_model.predict(record))
        except (ValueError,KeyError,ArithmeticError):
            continue
        if finite(value):
            values[str(side)]=value
    if not values:
        return None,None,values
    side,value=max(values.items(),key=lambda x:(x[1],x[0]))
    return (side,float(value),values) if value>0 else (None,float(value),values)


def _feature_values(row,predicates):
    out=[]
    for key,value in (row.get("features") or {}).items():
        lk=str(key).lower()
        if any(p(lk) for p in predicates) and finite(value):
            out.append(float(value))
    return out


def _raw_book_paths(root:Path):
    found=set()
    for base in (root,root.parent):
        for pattern in (
            "research/repricing_book/book_observations/*.jsonl*",
            "micro_maker/book_observations/*.jsonl*",
            "paper_v7_london_archives/**/research/repricing_book/book_observations/*.jsonl*",
            "paper_v7_london_archives/**/micro_maker/book_observations/*.jsonl*",
        ):
            for path in base.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    found.add(path.resolve())
    return sorted(found)


def attach_raw_pm_features(rows,root:Path,start_ns:int,end_ns:int):
    required={}
    markets=set()
    for row in rows:
        market=str(row["market_id"]); markets.add(market)
        required.setdefault(market,set()).update(
            str(x) for x in (row.get("yes_token_id"),row.get("no_token_id")) if x
        )
    index={}
    counts=Counter()
    lo_ms=start_ns//1_000_000-2_000
    hi_ms=end_ns//1_000_000+1_000
    for path in _raw_book_paths(root):
        for raw in m.robust_json_lines(path):
            if raw.get("schema")!="polymarket_v7_causal_book_observation_v1":
                continue
            counts["schema_rows"]+=1
            try:
                market=str(raw["market_id"]); token=str(raw["token_id"])
                wall=int(raw["receive_wall_ms"])
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
            if market not in markets or token not in required.get(market,set()):
                continue
            if wall<lo_ms or wall>hi_ms:
                continue
            if raw.get("valid") is not True or raw.get("lineage_continuous") is not True:
                counts["invalid_or_gap"]+=1; continue
            placement=raw.get("placement_features")
            if not isinstance(placement,dict):
                counts["missing_placement"]+=1; continue
            try:
                bid=float(raw["best_bid"]); ask=float(raw["best_ask"])
                bid_depth=float(raw.get("bid_depth_l1") or 0.0)
                ask_depth=float(raw.get("ask_depth_l1") or 0.0)
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
            if not (0<bid<ask<1 and bid_depth>=0 and ask_depth>=0):
                continue
            item={
                "wall_ms":wall,"receive_ns":wall*1_000_000,
                "best_bid":bid,"best_ask":ask,"bid_depth_l1":bid_depth,
                "ask_depth_l1":ask_depth,"placement":placement,
            }
            index.setdefault((market,token),[]).append(item)
            counts["accepted_rows"]+=1
    packed={}
    for key,seq in index.items():
        seq.sort(key=lambda x:x["receive_ns"])
        packed[key]={"rows":seq,"stamps":[x["receive_ns"] for x in seq]}

    out=[]; joined=0; ages=[]
    for original in rows:
        row=dict(original); row["features"]=dict(original.get("features") or {})
        market=str(row["market_id"]); decision=int(row["decision_ns"])
        selected={}
        for outcome,token_key in (("yes","yes_token_id"),("no","no_token_id")):
            token=str(row.get(token_key) or "")
            idx=packed.get((market,token))
            if not idx: continue
            pos=bisect_right(idx["stamps"],decision)-1
            if pos<0: continue
            cut=idx["rows"][pos]
            age_ms=(decision-cut["receive_ns"])/1e6
            if not 0<=age_ms<=1000: continue
            selected[outcome]=cut
            p=cut["placement"]
            prefix=f"research.pm_{outcome}_"
            for source,target in (
                ("spread_ticks","spread_ticks"),
                ("imbalance","imbalance"),
                ("ofi","ofi"),
                ("ew_vol_ticks","ew_vol_ticks"),
                ("short_return_ticks","short_return_ticks"),
                ("trade_intensity","trade_intensity"),
                ("cancel_intensity","cancel_intensity"),
                ("aggressive_buy_prints_per_second","aggressive_buy_prints_per_second"),
                ("aggressive_sell_prints_per_second","aggressive_sell_prints_per_second"),
                ("local_latency_ms","local_latency_ms"),
                ("microstructure_shadow_delta_250ms","microstructure_shadow_delta_250ms"),
            ):
                value=p.get(source)
                if finite(value): row["features"][prefix+target]=float(value)
            row["features"][f"research.pm_{outcome}_mid"]=.5*(cut["best_bid"]+cut["best_ask"])
            row["features"][f"research.pm_{outcome}_spread"]=cut["best_ask"]-cut["best_bid"]
            row["features"][f"research.maker_queue_{outcome}_queue_ahead"]=cut["bid_depth_l1"]
            row["features"][f"research.maker_queue_{outcome}_depth_l1"]=cut["bid_depth_l1"]
            row["features"][f"research.pm_{outcome}_book_depth_imbalance"]=(
                (cut["bid_depth_l1"]-cut["ask_depth_l1"])/
                max(1e-12,cut["bid_depth_l1"]+cut["ask_depth_l1"])
            )
            ages.append(age_ms)
        if "yes" in selected and "no" in selected:
            joined+=1
            row["features"]["research.pm_complete_set_mid"]=(
                .5*(selected["yes"]["best_bid"]+selected["yes"]["best_ask"])
                +.5*(selected["no"]["best_bid"]+selected["no"]["best_ask"])
            )
        out.append(row)
    return out,{
        "files":len(_raw_book_paths(root)),"counts":dict(counts),
        "rows":len(rows),"dual_token_joined":joined,
        "dual_token_join_rate":joined/len(rows) if rows else None,
        "age_ms_p50":statistics.median(ages) if ages else None,
    }


def residual_components(row):
    pm=_feature_values(row,(
        lambda k:"short_return_ticks" in k,
        lambda k:"pm_delta" in k,
        lambda k:"microstructure_shadow_delta" in k,
    ))
    external=_feature_values(row,(
        lambda k:any(x in k for x in (
            "binance_return_100ms","coinbase_return_100ms","bybit_return_100ms",
            "external.return_50ms","external.return_100ms","external.return_250ms",
        )),
    ))
    if not pm or not external:
        return None
    return statistics.fmean(pm),statistics.fmean(external)


def mean_sd(values):
    if len(values)<20:
        return None
    mean=statistics.fmean(values)
    sd=statistics.pstdev(values)
    if not finite(sd) or sd<=1e-12:
        return None
    return float(mean),float(sd)


def q75(values):
    seq=sorted(float(x) for x in values if finite(x))
    if not seq:
        return None
    return seq[min(len(seq)-1,max(0,math.ceil(.75*len(seq))-1))]


def fit_residual_rule(rows):
    pairs=[residual_components(r) for r in rows]
    pairs=[p for p in pairs if p is not None]
    if len(pairs)<40:
        return {"state":"INSUFFICIENT_DATA","pairs":len(pairs)}
    pm_stats=mean_sd([p[0] for p in pairs]); ex_stats=mean_sd([p[1] for p in pairs])
    if pm_stats is None or ex_stats is None:
        return {"state":"INSUFFICIENT_DATA","pairs":len(pairs)}
    residuals=[(p[0]-pm_stats[0])/pm_stats[1]-(p[1]-ex_stats[0])/ex_stats[1] for p in pairs]
    threshold=q75(abs(x) for x in residuals)
    return {
        "state":"READY","pairs":len(pairs),"pm_mean":pm_stats[0],"pm_sd":pm_stats[1],
        "external_mean":ex_stats[0],"external_sd":ex_stats[1],
        "abs_residual_q75":threshold,
    }


def residual_side(rule,row):
    if rule.get("state")!="READY":
        return None,None,{}
    pair=residual_components(row)
    if pair is None:
        return None,None,{}
    residual=(pair[0]-rule["pm_mean"])/rule["pm_sd"]-(pair[1]-rule["external_mean"])/rule["external_sd"]
    threshold=float(rule["abs_residual_q75"])
    if residual>=threshold:
        return "NO",-residual,{"residual":residual}
    if residual<=-threshold:
        return "YES",residual,{"residual":residual}
    return None,-abs(residual),{"residual":residual}


def _selected_outcome_mid(pair,side):
    if pair is None:
        return None
    prefix="yes" if side=="YES" else "no"
    bid=pair.get(prefix+"_best_bid"); ask=pair.get(prefix+"_best_ask")
    if not finite(bid) or not finite(ask):
        return None
    return .5*(float(bid)+float(ask))


def evaluate_cell(policy,selector,rows,session_cache,latency,horizon):
    stats=Counter()
    pnl=filled_shares=gross_markout=adverse_markout=spread_component=turnover=inventory_seconds=0.0
    signed_moves=[]
    selected_predictions=[]
    censor=Counter()
    for row in rows:
        stats["observations"]+=1
        session,reason=session_cache[str(row["decision_id"])]
        if session is None:
            censor[str(reason)]+=1; continue
        decision_ms=int(row["decision_ns"])/1e6
        origin=pair_asof_session(session,row,decision_ms)
        future=pair_asof_session(session,row,decision_ms+horizon)
        if origin is None or future is None:
            censor["PM_HORIZON_UNAVAILABLE"]+=1; continue
        if policy=="A0_BASELINE_NO_ADDED_ALPHA":
            side=da.selected_action_side(row); pred=None; aux={}
        else:
            side,pred,aux=selector(row,latency,horizon)
        if side is None:
            stats["no_trade"]+=1
            continue
        stats["selected_signals"]+=1
        econ,state=m.execute_side_cell(row,session,latency,horizon,side)
        if econ is None:
            censor[str(state)]+=1; continue
        stats["observed_actions"]+=1
        yes_move=float(future["pm_yes"])-float(origin["pm_yes"])
        signed=yes_move if side=="YES" else -yes_move
        signed_moves.append(signed)
        if signed>0: stats["direction_correct"]+=1
        elif signed<0: stats["direction_wrong"]+=1
        fill=float(econ.get("filled") or 0.0)
        value=float(econ.get("cash_pnl") or 0.0)
        pnl+=value
        if pred is not None and finite(pred): selected_predictions.append(float(pred))
        if fill<=0:
            stats["no_fill"]+=1
            continue
        stats["fills"]+=1
        filled_shares+=fill
        entry=float(econ["entry_price"]); exit_bid=float(econ["exit_bid"])
        executable=(exit_bid-entry)*fill
        gross_markout+=executable
        adverse_markout+=max(0.0,-executable)
        origin_mid=_selected_outcome_mid(origin,side)
        if origin_mid is not None:
            spread_component+=(origin_mid-entry)*fill
        exit_fill=float(econ.get("exit_filled") or 0.0)
        turnover+=fill*entry+exit_fill*exit_bid
        inventory_seconds+=fill*horizon/1000.0
        if executable<0: stats["toxic_fills"]+=1
        if value>0: stats["positive_pnl_fills"]+=1
        elif value<0: stats["negative_pnl_fills"]+=1
    return {
        "policy":policy,"entry_latency_ms":latency,"horizon_ms":horizon,
        "observations":stats["observations"],"selected_signals":stats["selected_signals"],
        "observed_actions":stats["observed_actions"],"fills":stats["fills"],
        "fill_probability":stats["fills"]/stats["observed_actions"] if stats["observed_actions"] else None,
        "future_pm_signed_move_mean":statistics.fmean(signed_moves) if signed_moves else None,
        "directional_accuracy":stats["direction_correct"]/(stats["direction_correct"]+stats["direction_wrong"]) if stats["direction_correct"]+stats["direction_wrong"] else None,
        "gross_executable_markout":gross_markout,
        "adverse_markout":adverse_markout,
        "spread_component":spread_component,
        "toxic_fill_probability":stats["toxic_fills"]/stats["fills"] if stats["fills"] else None,
        "net_pnl":pnl,
        "net_pnl_per_share":pnl/filled_shares if filled_shares else None,
        "filled_shares":filled_shares,
        "inventory_seconds":inventory_seconds,
        "turnover_notional":turnover,
        "mean_model_score":statistics.fmean(selected_predictions) if selected_predictions else None,
        "positive_pnl_fills":stats["positive_pnl_fills"],
        "negative_pnl_fills":stats["negative_pnl_fills"],
        "censoring":dict(censor),
    }


def monotonicity(rows,metric,x_name,group_names):
    groups={}
    for row in rows:
        if row.get(metric) is None:
            continue
        key=tuple(row[g] for g in group_names)
        groups.setdefault(key,[]).append((int(row[x_name]),float(row[metric])))
    out={}
    for key,points in groups.items():
        out["|".join(map(str,key))]=m._monotone_shape(points)
    return out


def run(root:Path,output:Path,code_sha:str,minimum_wall_ns:int):
    if len(code_sha)!=40 or any(c not in "0123456789abcdef" for c in code_sha):
        raise ValueError("exact SHA required")
    output.mkdir(parents=True,exist_ok=False)
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        raise ValueError("CAUSAL_DATASET_NOT_READY:"+str(data.get("input_state")))
    source=[r for r in data["decisions"] if da._valid_state(r)]
    sessions,tape_diag=stream_sessions(Path(root).resolve().parent,source)
    if not sessions:
        sessions,fallback=jsonl_sessions(Path(root).resolve(),source)
        tape_diag={**tape_diag,**fallback,"fallback":"JSONL_BOOK_OBSERVATIONS"}
    market_index=m.build_market_session_index(sessions)
    session_cache={str(r["decision_id"]):m.resolve_session(r,market_index) for r in source}
    window=m.select_two_hour_window(source,session_cache)
    start,end=int(window["start_ns"]),int(window["end_ns"])
    rows=[r for r in source if start<=int(r["decision_ns"])<end]
    cut=start+int(.60*WINDOW_NS)
    train=[r for r in rows if int(r["decision_ns"])<cut]
    oos=[r for r in rows if int(r["decision_ns"])>=cut]
    if not train or not oos:
        raise ValueError("TRAIN_OR_LOCKED_OOS_EMPTY")
    feature_paths=m.discover_feature_tapes(root)
    feature_index,feature_diag=m.load_feature_tape(feature_paths,start_ns=start,end_ns=end)
    rich,join_diag=m.attach_rich_state(rows,feature_index,delay_ms=0)
    rich,pm_join_diag=attach_raw_pm_features(rich,root,start,end)
    by_id={str(r["decision_id"]):r for r in rich}
    train_r=[by_id[str(r["decision_id"])] for r in train]
    oos_r=[by_id[str(r["decision_id"])] for r in oos]
    inventory=m.family_inventory(rich)

    models={}
    receipts={}
    for name,families in (
        ("A1_EXTERNAL_MOMENTUM",A1_FAMILIES),
        ("A2_PM_MICROSTRUCTURE",A2_FAMILIES),
        ("A3_EXTERNAL_PLUS_PM",A3_FAMILIES),
        ("A5_FULL_EXECUTION_ALPHA",A5_FAMILIES),
    ):
        try:
            model=fit_model(train_r,families)
            models[name]=model
            receipts[name]={
                "state":"READY","families":sorted(families),
                "feature_names":list(model.model_feature_names),
                "training_receipt":model.training_receipt,
            }
        except (ValueError,RuntimeError,ArithmeticError) as exc:
            receipts[name]={"state":"INSUFFICIENT_DATA","reason":type(exc).__name__+":"+str(exc),"families":sorted(families)}

    residual=fit_residual_rule(train_r)
    selectors={
        name:(lambda row,l,h,model=model:model_side(model,row,l,h))
        for name,model in models.items()
    }
    selectors["A4_RESIDUAL_MEAN_REVERSION"]=lambda row,l,h:residual_side(residual,row)

    grid=[]
    for policy in POLICIES:
        selector=selectors.get(policy)
        if policy!="A0_BASELINE_NO_ADDED_ALPHA" and selector is None:
            continue
        for latency in LATENCIES:
            for horizon in EXITS:
                grid.append(evaluate_cell(policy,selector,oos_r,session_cache,latency,horizon))

    dynamic={"state":"INSUFFICIENT_DATA"}
    try:
        dm=DynamicExitValueModel().fit(train_r)
        dynamic={"state":"READY","training_receipt":dm.training_receipt,
                 "diagnostic":summarize_dynamic_exit(dm,oos_r,position_size=SIZE)}
    except (ValueError,RuntimeError) as exc:
        dynamic={"state":"INSUFFICIENT_DATA","reason":type(exc).__name__+":"+str(exc)}

    mono={
        "latency_net_pnl_per_share":monotonicity(grid,"net_pnl_per_share","entry_latency_ms",("policy","horizon_ms")),
        "horizon_net_pnl_per_share":monotonicity(grid,"net_pnl_per_share","horizon_ms",("policy","entry_latency_ms")),
        "latency_fill_probability":monotonicity(grid,"fill_probability","entry_latency_ms",("policy","horizon_ms")),
        "horizon_toxic_fill_probability":monotonicity(grid,"toxic_fill_probability","horizon_ms",("policy","entry_latency_ms")),
        "horizon_future_pm_signed_move":monotonicity(grid,"future_pm_signed_move_mean","horizon_ms",("policy","entry_latency_ms")),
    }

    # OOS-only descriptive shortlist; no automatic promotion and no live use.
    by_policy={}
    for policy in POLICIES:
        cells=[r for r in grid if r["policy"]==policy and r["net_pnl_per_share"] is not None]
        if cells:
            best=max(cells,key=lambda r:(r["net_pnl_per_share"],r["fills"],-r["entry_latency_ms"],-r["horizon_ms"]))
            by_policy[policy]={"descriptive_best_cell":best}
        else:
            by_policy[policy]={"descriptive_best_cell":None}

    manifest={
        "schema":SCHEMA+"_manifest",**SAFETY_PLUS,"code_sha":code_sha,
        "window_start_ns":start,"window_end_ns":end,"window_seconds":7200,
        "split":{"train_60":{"start_ns":start,"end_ns":cut,"rows":len(train)},
                 "locked_oos_40":{"start_ns":cut,"end_ns":end,"rows":len(oos)}},
        "selection":window,"latencies_ms":list(LATENCIES),"horizons_ms":list(EXITS),
        "size_shares":SIZE,"data_sha256":data.get("data_sha256"),
        "feature_join":join_diag,"raw_pm_feature_join":pm_join_diag,
        "continuous_pm_tape":tape_diag,"feature_inventory":inventory,
        "oos_lock_semantics":"NO_FIT_THRESHOLD_SCALER_OR_FEATURE_SELECTION_ON_FINAL_40_PERCENT",
    }
    atomic_json(output/"01_manifest.json",manifest)
    atomic_json(output/"02_training_receipts.json",{"schema":SCHEMA+"_training",**SAFETY_PLUS,"models":receipts,"A4_residual":residual})
    atomic_json(output/"03_locked_oos_grid.json",{"schema":SCHEMA+"_grid",**SAFETY_PLUS,"rows":grid})
    atomic_json(output/"04_monotonicity.json",{"schema":SCHEMA+"_monotonicity",**SAFETY_PLUS,**mono})
    atomic_json(output/"05_dynamic_exit.json",{"schema":SCHEMA+"_dynamic_exit",**SAFETY_PLUS,**dynamic})
    atomic_json(output/"06_descriptive_cells.json",{"schema":SCHEMA+"_descriptive_cells",**SAFETY_PLUS,"policies":by_policy,"promotion":"NONE"})

    fields=[
        "policy","entry_latency_ms","horizon_ms","observations","selected_signals",
        "observed_actions","fills","fill_probability","future_pm_signed_move_mean",
        "directional_accuracy","gross_executable_markout","adverse_markout",
        "spread_component","toxic_fill_probability","net_pnl","net_pnl_per_share",
        "filled_shares","inventory_seconds","turnover_notional","mean_model_score",
    ]
    with (output/"07_locked_oos_table.csv").open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        for row in grid:
            writer.writerow({k:row.get(k) for k in fields})

    summary={
        "schema":SCHEMA+"_summary",**SAFETY_PLUS,
        "window":{"start_ns":start,"end_ns":end,"train_rows":len(train),"locked_oos_rows":len(oos)},
        "policies":by_policy,"residual_rule":residual,
        "feature_family_counts":{k:len(v or []) for k,v in inventory.get("families",{}).items()},
        "raw_pm_feature_join":pm_join_diag,
        "interpretation":"A0-A5_FIXED_HORSE_RACE;FINAL_40_PERCENT_LOCKED_OOS;NO_AUTOMATIC_PROMOTION",
    }
    atomic_json(output/"00_summary.json",summary)
    print("A0_A5_READY="+json.dumps(summary,sort_keys=True,separators=(",",":")))
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    p.add_argument("--code-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    try:
        run(a.root,a.output_dir,a.code_sha,a.minimum_wall_ns)
    except (OSError,ValueError,RuntimeError) as exc:
        p.exit(2,type(exc).__name__+":"+str(exc)+"\n")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
