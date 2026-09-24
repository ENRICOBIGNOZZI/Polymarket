#!/usr/bin/env python3
"""Exact A0-A5 alpha-driven maker horse race on one causal 2H window.

The experiment freezes maker mechanics and varies only the information set.
TRAIN is the first 60% of the selected 2H window. The final 40% is one locked
OOS block: no model/threshold selection is performed on it.

Maker mechanics are fixed in this stage:
- BUY-side passive JOIN quotes on YES and NO tokens;
- 5 shares target size;
- 500ms quote TTL;
- queue ahead = 1.25 x observed L1 bid depth;
- no maker rebate/reward credit;
- exit crosses the observed future bid and pays the recorded taker fee;
- missing/gapped execution or markout evidence is censored, never zero-imputed.

A0: no alpha, quote both outcomes.
A1: external momentum/lead-lag.
A2: PM microstructure.
A3: external + PM.
A4: PM minus external-fair residual; coefficient sign is learned, so
    continuation vs mean reversion is decided by data.
A5: full execution alpha using the explicit execution feature contract.

This module has ZERO execution authority and never writes to the canonical ledger.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable

import numpy as np

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, fee_per_share
from research.walk_forward_v3 import direct_action as da
from research.walk_forward_v3.btc_compact_equity import (
    EXITS, LATENCIES, SIZE, jsonl_sessions, pair_asof_session, side_state, stream_sessions,
)
from research.walk_forward_v3.multi_alpha_2h import (
    WINDOW_NS, attach_rich_state, build_market_session_index, discover_feature_tapes,
    finite, load_feature_tape, resolve_session, select_two_hour_window, _monotone_shape,
)

SCHEMA="polymarket_v7_maker_a0_a5_2h_v1"
POLICIES=("A0_BASELINE","A1_EXTERNAL","A2_PM","A3_EXTERNAL_PM","A4_RESIDUAL","A5_FULL_EXECUTION")
TRAIN_FRACTION=0.60
QUOTE_TTL_MS=500
QUEUE_AHEAD_MULTIPLIER=1.25
TARGET_SHARES=5.0
RIDGE=8.0
MIN_TRAIN_TARGETS=50
MAX_FEATURES=64

SAFETY_PLUS={
    **SAFETY,
    "automatic_promotion":False,
    "execution_authority":False,
    "research_only":True,
    "canonical_ledger_writes":False,
}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,sort_keys=True,separators=(",",":"),allow_nan=False,
    ).encode()).hexdigest()


def robust_json_lines(path: Path):
    try:
        with path.open("rb") as f:
            magic=f.read(2)
    except OSError:
        return
    opener=gzip.open if magic==b"\x1f\x8b" else open
    try:
        with opener(path,"rt",encoding="utf-8") as f:
            for line in f:
                if not line.endswith("\n"):
                    continue
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict):
                    yield value
    except (OSError,UnicodeDecodeError):
        return


def safe_feature_name(name: str) -> bool:
    n=name.lower()
    return not any(x in n for x in (
        "realized_","future_","label_","outcome_","post_fill","actual_markout",
        "realized_markout","realized_pnl","target_",
    ))


def external_feature(name: str) -> bool:
    n=name.lower()
    if not safe_feature_name(n):
        return False
    return any(x in n for x in (
        "signal_return","signal_age","parent_shock",
        "binance_return","coinbase_return","bybit_return",
        "external.return_","tape.external.return_",
        "dispersion","fresh_venue","agreement","venue_leader","venue_laggard",
        "composite_price","external.aggregate_ofi","external.aggregate_trade_imbalance",
        "tape.external.aggregate_ofi","tape.external.aggregate_trade_imbalance",
        "leader_features",
    ))


def pm_feature(name: str) -> bool:
    n=name.lower()
    if not safe_feature_name(n) or external_feature(n):
        return False
    return (
        n.startswith("tape.pm_")
        or any(x in n for x in (
            "pm_yes_mid","pm_no_mid","pm_complete_set","pm_yes_spread","pm_yes_imbalance",
            "depth_imbalance","book_imbalance","short_return_ticks","spread_ticks",
            "aggressive_buy","aggressive_sell","trade_intensity","cancel_intensity",
            "microstructure_shadow_delta","microprice",
        ))
    )


def full_execution_feature(name: str) -> bool:
    n=name.lower()
    if not safe_feature_name(n):
        return False
    if external_feature(n) or pm_feature(n):
        return True
    return any(x in n for x in (
        "queue_ahead","queue_confidence","fill_probability","fillability",
        "quote_lifetime","distance_from_touch","tte","time_to_resolution",
        "oracle","reference","spot_minus_oracle","distance_to_reference",
        "volatility","native_vol","realized_vol","vol_fast","vol_slow","jump",
        "funding","open_interest","liquidat","perp","basis","deribit","implied_vol",
        "local_latency","latency_ms","adverse_selection","toxic_fill_probability",
        "predicted_markout",
    ))


def _variance(values: list[float]) -> float:
    if len(values)<2:return 0.0
    m=statistics.fmean(values)
    return statistics.fmean((x-m)*(x-m) for x in values)


def select_names(rows: list[dict[str,Any]], predicate, *, maximum=MAX_FEATURES) -> list[str]:
    stats: dict[str,list[float]]=defaultdict(list)
    for row in rows:
        for name,value in (row.get("features") or {}).items():
            if predicate(str(name)) and finite(value):
                stats[str(name)].append(float(value))
    scored=[]
    n=max(1,len(rows))
    for name,values in stats.items():
        coverage=len(values)/n
        variance=_variance(values)
        if coverage<0.10 or variance<=1e-18:
            continue
        scored.append((coverage,math.log1p(variance),name))
    scored.sort(reverse=True)
    return [name for _,__,name in scored[:maximum]]


def base_context(row: dict[str,Any]) -> dict[str,float]:
    out={
        "ctx.pm_yes":float(((row.get("pair") or {}).get("pm_yes") or 0.5)),
        "ctx.tte_s":float(row.get("tte_ns") or 0)/1e9,
        "ctx.signal_age_ms":float(row.get("signal_age_ns") or 0)/1e6,
    }
    out["asset::"+str(row.get("asset") or "UNKNOWN")]=1.0
    out["contract::"+str(row.get("horizon") or "UNKNOWN")]=1.0
    return out


def feature_dict(row: dict[str,Any], names: list[str]) -> dict[str,float|None]:
    f=row.get("features") or {}
    out={name:(float(f[name]) if name in f and finite(f[name]) else None) for name in names}
    out.update(base_context(row))
    return out


class Ridge:
    def __init__(self, names: list[str], ridge: float=RIDGE):
        self.names=tuple(names)
        self.ridge=float(ridge)
        self.means={}
        self.scales={}
        self.coef=None

    def fit(self, rows: list[dict[str,float|None]], targets: list[float]):
        if len(rows)!=len(targets) or len(rows)<MIN_TRAIN_TARGETS:
            raise ValueError("INSUFFICIENT_RIDGE_TARGETS")
        names=list(self.names)
        for name in names:
            values=[float(r[name]) for r in rows if r.get(name) is not None and finite(r.get(name))]
            self.means[name]=statistics.fmean(values) if values else 0.0
            sd=math.sqrt(_variance(values)) if len(values)>1 else 0.0
            self.scales[name]=sd if sd>1e-12 else 1.0
        p=1+2*len(names)
        gram=np.zeros((p,p),dtype=float)
        rhs=np.zeros(p,dtype=float)
        for row,y in zip(rows,targets):
            x=np.zeros(p,dtype=float);x[0]=1.0
            j=1
            for name in names:
                value=row.get(name)
                missing=value is None or not finite(value)
                x[j]=0.0 if missing else (float(value)-self.means[name])/self.scales[name]
                x[j+1]=1.0 if missing else 0.0
                j+=2
            gram+=np.outer(x,x);rhs+=x*float(y)
        penalty=np.eye(p)*self.ridge
        penalty[0,0]=1e-9
        self.coef=np.linalg.solve(gram+penalty,rhs)
        return self

    def predict(self,row: dict[str,float|None]) -> float:
        if self.coef is None: raise RuntimeError("RIDGE_NOT_FIT")
        x=np.zeros(len(self.coef),dtype=float);x[0]=1.0
        j=1
        for name in self.names:
            value=row.get(name)
            missing=value is None or not finite(value)
            x[j]=0.0 if missing else (float(value)-self.means[name])/self.scales[name]
            x[j+1]=1.0 if missing else 0.0
            j+=2
        return float(np.dot(self.coef,x))


def split_60_40(rows: list[dict[str,Any]], start_ns: int):
    cut=int(start_ns+TRAIN_FRACTION*WINDOW_NS)
    end=int(start_ns+WINDOW_NS)
    train=[r for r in rows if start_ns<=int(r["decision_ns"])<cut]
    oos=[r for r in rows if cut<=int(r["decision_ns"])<end]
    return train,oos,cut


def current_pair(row,session):
    return pair_asof_session(session,row,int(row["decision_ns"])/1e6)


def future_delta(row,session,latency_ms,horizon_ms):
    now=current_pair(row,session)
    if now is None:return None
    target=int(row["decision_ns"])/1e6+int(latency_ms)+int(horizon_ms)
    future=pair_asof_session(session,row,target)
    if future is None:return None
    if int(now.get("connection_epoch") or 0)!=int(future.get("connection_epoch") or 0):
        return None
    return float(future["pm_yes"])-float(now["pm_yes"])


def discover_trade_paths(root: Path) -> list[Path]:
    candidates=[]
    roots=(root,root.parent)
    patterns=(
        "research/repricing_book/fillability_ws.jsonl*",
        "micro_maker/fillability_ws.jsonl*",
        "paper_v7_london_archives/**/research/repricing_book/fillability_ws.jsonl*",
        "paper_v7_london_archives/**/micro_maker/fillability_ws.jsonl*",
    )
    for base in roots:
        if not base.exists():continue
        for pattern in patterns:
            candidates.extend(base.glob(pattern))
    return sorted({p.resolve() for p in candidates if p.is_file() and not p.is_symlink()})


def load_trades(root: Path, rows: list[dict[str,Any]], start_ns: int, end_ns: int):
    market_tokens=defaultdict(set)
    for row in rows:
        market=str(row["market_id"])
        for token in (row.get("yes_token_id"),row.get("no_token_id")):
            if token: market_tokens[market].add(str(token))
    lo=start_ns//1_000_000-1000
    hi=end_ns//1_000_000+max(LATENCIES)+QUOTE_TTL_MS+max(EXITS)+1000
    out=defaultdict(list);counts=Counter()
    for path in discover_trade_paths(root):
        counts["files"]+=1
        for raw in robust_json_lines(path):
            if raw.get("schema")!="polymarket_v7_maker_fillability_ws_trade_v1":
                continue
            counts["schema_rows"]+=1
            if raw.get("paper_only") is not True or raw.get("authenticated_execution") is not False or raw.get("real_order_submission") is not False:
                counts["authority_rejected"]+=1;continue
            try:
                wall=int(raw["receive_wall_ms"]);market=str(raw["market_id"]);token=str(raw["token_id"])
                price=float(raw["price"]);size=float(raw["size"]);epoch=int(raw.get("connection_epoch") or 0)
                side=str(raw.get("aggressor_side") or "").upper()
            except (KeyError,TypeError,ValueError,OverflowError):
                counts["invalid"]+=1;continue
            if not lo<=wall<=hi or token not in market_tokens.get(market,set()):
                continue
            if raw.get("lineage_continuous") is not True or side not in ("BUY","SELL") or not (0<price<1 and size>0 and epoch>0):
                counts["invalid"]+=1;continue
            out[(market,token)].append((wall,epoch,side,price,size))
            counts["accepted"]+=1
    indexed={}
    for key,seq in out.items():
        seq.sort()
        indexed[key]={"rows":seq,"stamps":[x[0] for x in seq]}
    return indexed,dict(counts)


def maker_fill(row,session,trades,side,latency_ms):
    origin_ms=int(row["decision_ns"])/1e6
    arrival_ms=origin_ms+int(latency_ms)
    pair=pair_asof_session(session,row,arrival_ms)
    if pair is None:return None,"ARRIVAL_BOOK_UNAVAILABLE"
    if row.get("epoch") and int(pair.get("connection_epoch") or 0)!=int(row["epoch"]):
        return None,"ARRIVAL_EPOCH_MISMATCH"
    state=side_state(pair,side)
    if state is None:return None,"ARRIVAL_SIDE_UNAVAILABLE"
    token=str(row.get("yes_token_id") if side=="YES" else row.get("no_token_id") or "")
    if not token:return None,"TOKEN_ID_UNAVAILABLE"
    price=float(state["bid"])
    tick=float(row.get("tick") or 0.01)
    ahead=max(0.0,float(state["bid_depth"])*QUEUE_AHEAD_MULTIPLIER)
    idx=trades.get((str(row["market_id"]),token))
    end_ms=arrival_ms+QUOTE_TTL_MS
    if idx is None:
        return {"fills":[],"quote_price":price,"arrival_pair":pair,"posted_shares":TARGET_SHARES},"OBSERVED_NO_TRADES"
    left=bisect_right(idx["stamps"],arrival_ms)
    right=bisect_right(idx["stamps"],end_ms)
    remaining=TARGET_SHARES
    fills=[]
    for wall,epoch,aggressor,trade_price,size in idx["rows"][left:right]:
        if epoch!=int(pair.get("connection_epoch") or 0):
            continue
        if aggressor!="SELL" or abs(float(trade_price)-price)>max(1e-9,tick/2):
            continue
        q=float(size)
        consume=min(ahead,q);ahead-=consume;q-=consume
        if q<=0:continue
        fill=min(remaining,q)
        if fill>0:
            fills.append({"wall_ms":float(wall),"shares":float(fill),"price":price})
            remaining-=fill
        if remaining<=1e-12:break
    return {"fills":fills,"quote_price":price,"arrival_pair":pair,"posted_shares":TARGET_SHARES},"OBSERVED"


def maker_value(row,session,fill_result,horizon_ms):
    fills=fill_result["fills"]
    if not fills:
        return {
            "economic_observed":True,"filled_shares":0.0,"net_pnl":0.0,
            "gross_spread_capture":0.0,"gross_markout":0.0,"executable_markout":0.0,
            "adverse_markout":0.0,"toxic":False,"inventory_seconds":0.0,
        },"NO_FILL"
    side=fill_result["side"]
    arrival_state=side_state(fill_result["arrival_pair"],side)
    if arrival_state is None:return None,"ARRIVAL_STATE_LOST"
    arrival_mid=0.5*(float(arrival_state["bid"])+float(arrival_state["ask"]))
    total_fill=sum(float(x["shares"]) for x in fills)
    net=gross_spread=gross_markout=exec_markout=adverse=inventory=0.0
    for fill in fills:
        target_ms=float(fill["wall_ms"])+int(horizon_ms)
        pair=pair_asof_session(session,row,target_ms)
        if pair is None:return None,"FUTURE_BOOK_UNAVAILABLE"
        future=side_state(pair,side)
        if future is None:return None,"FUTURE_SIDE_UNAVAILABLE"
        q=float(fill["shares"])
        if float(future["bid_depth"])+1e-12<q:
            return None,"FUTURE_EXIT_DEPTH_INSUFFICIENT"
        price=float(fill["price"])
        future_bid=float(future["bid"])
        future_mid=.5*(float(future["bid"])+float(future["ask"]))
        exit_fee=q*fee_per_share(row,future_bid)
        spread_ps=arrival_mid-price
        gross_ps=future_mid-price
        executable_ps=future_bid-price
        gross_spread+=q*spread_ps
        gross_markout+=q*gross_ps
        exec_markout+=q*executable_ps
        adverse+=q*max(0.0,-executable_ps)
        net+=q*executable_ps-exit_fee
        inventory+=q*float(horizon_ms)/1000.0
    return {
        "economic_observed":True,"filled_shares":total_fill,"net_pnl":net,
        "gross_spread_capture":gross_spread,"gross_markout":gross_markout,
        "executable_markout":exec_markout,"adverse_markout":adverse,
        "toxic":net<0,"inventory_seconds":inventory,
    },"OBSERVED"


def prediction_names(train_rows):
    ext=select_names(train_rows,external_feature)
    pm=select_names(train_rows,pm_feature)
    full=select_names(train_rows,full_execution_feature)
    return ext,pm,full


def train_external_fair(train_rows,session_cache,ext_names):
    records=[];targets=[]
    for row in train_rows:
        session,_=session_cache[str(row["decision_id"])]
        if session is None:continue
        pair=current_pair(row,session)
        if pair is None:continue
        records.append(feature_dict(row,ext_names))
        targets.append(float(pair["pm_yes"]))
    names=sorted(set(ext_names)|{k for r in records for k in r if k.startswith(("asset::","contract::","ctx."))})
    model=Ridge(names).fit([{k:r.get(k) for k in names} for r in records],targets)
    return model,names


def residual_features(row,session,external_model,external_names):
    pair=current_pair(row,session)
    if pair is None:return None
    base=feature_dict(row,external_names)
    fair=min(1.0,max(0.0,external_model.predict(base)))
    residual=float(pair["pm_yes"])-fair
    return {
        "residual.pm_minus_external":residual,
        "residual.abs":abs(residual),
        "residual.external_fair":fair,
        "residual.pm_yes":float(pair["pm_yes"]),
        "ctx.tte_s":float(row.get("tte_ns") or 0)/1e9,
        "asset::"+str(row.get("asset") or "UNKNOWN"):1.0,
        "contract::"+str(row.get("horizon") or "UNKNOWN"):1.0,
    }


def fit_cell_models(train_rows,session_cache,latency,horizon,ext_names,pm_names,full_names,external_model):
    specs={
        "A1_EXTERNAL":ext_names,
        "A2_PM":pm_names,
        "A3_EXTERNAL_PM":sorted(set(ext_names)|set(pm_names)),
        "A5_FULL_EXECUTION":full_names,
    }
    models={}
    receipts={}
    for policy,names0 in specs.items():
        records=[];targets=[]
        for row in train_rows:
            session,_=session_cache[str(row["decision_id"])]
            if session is None:continue
            y=future_delta(row,session,latency,horizon)
            if y is None:continue
            rec=feature_dict(row,names0)
            if policy=="A5_FULL_EXECUTION":
                pair=current_pair(row,session)
                if pair is None:continue
                yes=side_state(pair,"YES");no=side_state(pair,"NO")
                rec.update({
                    "action.latency_ms":float(latency),
                    "action.horizon_ms":float(horizon),
                    "action.quote_ttl_ms":float(QUOTE_TTL_MS),
                    "action.queue_multiplier":float(QUEUE_AHEAD_MULTIPLIER),
                    "state.yes_bid_depth":None if yes is None else yes["bid_depth"],
                    "state.no_bid_depth":None if no is None else no["bid_depth"],
                    "state.yes_spread":None if yes is None else yes["ask"]-yes["bid"],
                    "state.no_spread":None if no is None else no["ask"]-no["bid"],
                })
            records.append(rec);targets.append(float(y))
        names=sorted({k for rec in records for k in rec})
        try:
            model=Ridge(names).fit([{k:r.get(k) for k in names} for r in records],targets)
            models[policy]=model
            receipts[policy]={"state":"READY","training_targets":len(targets),"features":names}
        except (ValueError,np.linalg.LinAlgError) as exc:
            receipts[policy]={"state":"INSUFFICIENT_DATA","reason":str(exc),"training_targets":len(targets),"features":names}

    records=[];targets=[]
    for row in train_rows:
        session,_=session_cache[str(row["decision_id"])]
        if session is None:continue
        y=future_delta(row,session,latency,horizon)
        if y is None:continue
        rec=residual_features(row,session,external_model,ext_names)
        if rec is None:continue
        records.append(rec);targets.append(float(y))
    names=sorted({k for rec in records for k in rec})
    try:
        models["A4_RESIDUAL"]=Ridge(names).fit([{k:r.get(k) for k in names} for r in records],targets)
        receipts["A4_RESIDUAL"]={"state":"READY","training_targets":len(targets),"features":names}
    except (ValueError,np.linalg.LinAlgError) as exc:
        receipts["A4_RESIDUAL"]={"state":"INSUFFICIENT_DATA","reason":str(exc),"training_targets":len(targets),"features":names}
    return models,receipts


def predict_policy(policy,row,session,model,ext_names,pm_names,full_names,external_model,latency,horizon):
    if policy=="A0_BASELINE":return None
    if policy=="A1_EXTERNAL":rec=feature_dict(row,ext_names)
    elif policy=="A2_PM":rec=feature_dict(row,pm_names)
    elif policy=="A3_EXTERNAL_PM":rec=feature_dict(row,sorted(set(ext_names)|set(pm_names)))
    elif policy=="A4_RESIDUAL":
        rec=residual_features(row,session,external_model,ext_names)
        if rec is None:return None
    elif policy=="A5_FULL_EXECUTION":
        rec=feature_dict(row,full_names)
        pair=current_pair(row,session)
        if pair is None:return None
        yes=side_state(pair,"YES");no=side_state(pair,"NO")
        rec.update({
            "action.latency_ms":float(latency),"action.horizon_ms":float(horizon),
            "action.quote_ttl_ms":float(QUOTE_TTL_MS),"action.queue_multiplier":float(QUEUE_AHEAD_MULTIPLIER),
            "state.yes_bid_depth":None if yes is None else yes["bid_depth"],
            "state.no_bid_depth":None if no is None else no["bid_depth"],
            "state.yes_spread":None if yes is None else yes["ask"]-yes["bid"],
            "state.no_spread":None if no is None else no["ask"]-no["bid"],
        })
    else:return None
    return model.predict(rec)


def empty_stats():
    return {
        "observations":0,"selected_quote_episodes":0,"execution_observed":0,"economic_observed":0,
        "fill_episodes":0,"filled_shares":0.0,"posted_shares":0.0,"net_pnl":0.0,
        "gross_spread_capture":0.0,"gross_markout":0.0,"executable_markout":0.0,
        "adverse_markout":0.0,"toxic_fills":0,"inventory_seconds":0.0,
        "direction_correct":0,"direction_total":0,"future_pm_move_sum":0.0,
        "censoring":Counter(),
    }


def finalize_stats(s):
    fills=int(s["fill_episodes"]);selected=int(s["selected_quote_episodes"])
    econ=int(s["economic_observed"]);shares=float(s["filled_shares"]);posted=float(s["posted_shares"])
    return {
        **{k:(dict(v) if isinstance(v,Counter) else v) for k,v in s.items()},
        "fill_probability":fills/s["execution_observed"] if s["execution_observed"] else None,
        "direction_accuracy":s["direction_correct"]/s["direction_total"] if s["direction_total"] else None,
        "mean_future_pm_move":s["future_pm_move_sum"]/s["direction_total"] if s["direction_total"] else None,
        "net_pnl_per_posted_share":s["net_pnl"]/posted if posted else None,
        "net_pnl_per_fill_share":s["net_pnl"]/shares if shares else None,
        "gross_markout_per_fill_share":s["gross_markout"]/shares if shares else None,
        "executable_markout_per_fill_share":s["executable_markout"]/shares if shares else None,
        "gross_spread_capture_per_fill_share":s["gross_spread_capture"]/shares if shares else None,
        "toxic_fill_probability":s["toxic_fills"]/fills if fills else None,
        "mean_inventory_seconds_per_filled_share":s["inventory_seconds"]/shares if shares else None,
        "turnover_filled_per_posted":shares/posted if posted else None,
        "economic_observation_rate":econ/selected if selected else None,
    }


def evaluate_cell(oos_rows,session_cache,trades,models,latency,horizon,ext_names,pm_names,full_names,external_model):
    stats={p:empty_stats() for p in POLICIES}
    cooldown={p:{} for p in POLICIES}
    for row in sorted(oos_rows,key=lambda r:(int(r["decision_ns"]),str(r["decision_id"]))):
        session,_=session_cache[str(row["decision_id"])]
        if session is None:
            for p in POLICIES:stats[p]["censoring"]["NO_SESSION"]+=1
            continue
        actual=future_delta(row,session,latency,horizon)
        predictions={"A0_BASELINE":None}
        for p in POLICIES[1:]:
            model=models.get(p)
            predictions[p]=None if model is None else predict_policy(
                p,row,session,model,ext_names,pm_names,full_names,external_model,latency,horizon)
        for p in POLICIES:
            st=stats[p];st["observations"]+=1
            pred=predictions[p]
            if p!="A0_BASELINE" and pred is None:
                st["censoring"]["PREDICTION_UNAVAILABLE"]+=1;continue
            if p!="A0_BASELINE" and actual is not None and abs(actual)>1e-15 and abs(pred)>1e-15:
                st["direction_total"]+=1
                st["direction_correct"]+=int((pred>0)==(actual>0))
                st["future_pm_move_sum"]+=float(actual)
            quote_sides=("YES","NO") if p=="A0_BASELINE" else (("YES",) if pred>=0 else ("NO",))
            for side in quote_sides:
                token=str(row.get("yes_token_id") if side=="YES" else row.get("no_token_id") or "")
                key=(str(row["market_id"]),token)
                decision_ms=int(row["decision_ns"])//1_000_000
                if decision_ms<cooldown[p].get(key,-1):
                    continue
                cooldown[p][key]=decision_ms+latency+QUOTE_TTL_MS
                st["selected_quote_episodes"]+=1;st["posted_shares"]+=TARGET_SHARES
                fr,state=maker_fill(row,session,trades,side,latency)
                if fr is None:
                    st["censoring"][state]+=1;continue
                fr["side"]=side
                st["execution_observed"]+=1
                if fr["fills"]:st["fill_episodes"]+=1
                value,vstate=maker_value(row,session,fr,horizon)
                if value is None:
                    st["censoring"][vstate]+=1;continue
                st["economic_observed"]+=1
                for k in ("filled_shares","net_pnl","gross_spread_capture","gross_markout","executable_markout","adverse_markout","inventory_seconds"):
                    st[k]+=float(value[k])
                st["toxic_fills"]+=int(bool(value["toxic"]) and value["filled_shares"]>0)
    return {p:finalize_stats(s) for p,s in stats.items()}


def monotonicity(grid):
    out={}
    metrics=("net_pnl_per_posted_share","net_pnl_per_fill_share","fill_probability","toxic_fill_probability","direction_accuracy")
    for policy in POLICIES:
        out[policy]={"latency":{},"holding_horizon":{}}
        for metric in metrics:
            out[policy]["latency"][metric]={}
            for h in EXITS:
                out[policy]["latency"][metric][str(h)]=_monotone_shape([
                    (l,(grid.get(policy,{}).get(f"{l}::{h}") or {}).get(metric)) for l in LATENCIES
                ])
            out[policy]["holding_horizon"][metric]={}
            for l in LATENCIES:
                out[policy]["holding_horizon"][metric][str(l)]=_monotone_shape([
                    (h,(grid.get(policy,{}).get(f"{l}::{h}") or {}).get(metric)) for h in EXITS
                ])
    return out


def run(root: Path, output: Path, code_sha: str, minimum_wall_ns: int):
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":raise ValueError("CAUSAL_DATASET_NOT_READY:"+str(data.get("input_state")))
    source=[r for r in data["decisions"] if da._valid_state(r)]
    sessions,tape_diag=stream_sessions(root.resolve().parent,source)
    if not sessions:
        sessions,fallback=jsonl_sessions(root.resolve(),source)
        tape_diag={**tape_diag,**fallback,"fallback":"JSONL_BOOK_OBSERVATIONS"}
    market_index=build_market_session_index(sessions)
    session_cache={str(r["decision_id"]):resolve_session(r,market_index) for r in source}
    window=select_two_hour_window(source,session_cache)
    start_ns,end_ns=int(window["start_ns"]),int(window["end_ns"])
    rows=[r for r in source if start_ns<=int(r["decision_ns"])<end_ns]
    paths=discover_feature_tapes(root)
    feature_index,feature_diag=load_feature_tape(paths,start_ns=start_ns,end_ns=end_ns)
    rich,join_diag=attach_rich_state(rows,feature_index,delay_ms=0)
    train,oos,cut=split_60_40(rich,start_ns)
    if not train or not oos:raise ValueError("EMPTY_60_40_SPLIT")
    ext_names,pm_names,full_names=prediction_names(train)
    if not ext_names:raise ValueError("NO_EXTERNAL_FEATURES")
    if not pm_names:raise ValueError("NO_PM_FEATURES")
    external_model,external_level_names=train_external_fair(train,session_cache,ext_names)
    trades,trade_diag=load_trades(root,rows,start_ns,end_ns)
    if not trades:raise ValueError("NO_CAUSAL_MAKER_TRADES")

    grid={p:{} for p in POLICIES};receipts={}
    for latency in LATENCIES:
        for horizon in EXITS:
            if horizon<=latency:continue
            models,receipt=fit_cell_models(
                train,session_cache,latency,horizon,ext_names,pm_names,full_names,external_model)
            receipts[f"{latency}::{horizon}"]=receipt
            cell=evaluate_cell(
                oos,session_cache,trades,models,latency,horizon,
                ext_names,pm_names,full_names,external_model)
            for policy,value in cell.items():grid[policy][f"{latency}::{horizon}"]=value

    output.mkdir(parents=True,exist_ok=False)
    manifest={
        "schema":SCHEMA+"_manifest",**SAFETY_PLUS,
        "code_sha":code_sha,"window_start_ns":start_ns,"window_end_ns":end_ns,
        "window_seconds":7200,"train_end_ns":cut,
        "split":{"TRAIN_60":len(train),"LOCKED_OOS_40":len(oos)},
        "selection":window,"feature_join":join_diag,"feature_tape":feature_diag,
        "trade_tape":trade_diag,"book_tape":tape_diag,
        "maker_mechanics":{
            "placement":"JOIN","target_shares":TARGET_SHARES,"quote_ttl_ms":QUOTE_TTL_MS,
            "queue_ahead_multiplier":QUEUE_AHEAD_MULTIPLIER,
            "maker_entry_fee_credit":0.0,"rebate_credit":0.0,
            "exit":"OBSERVED_FUTURE_BID_WITH_RECORDED_TAKER_FEE",
            "spread_capture_semantics":"EMBEDDED_IN_EXECUTABLE_MARKOUT_NOT_DOUBLE_COUNTED_IN_NET_PNL",
        },
        "latencies_ms":list(LATENCIES),"holding_horizons_ms":list(EXITS),
        "feature_sets":{"A1_EXTERNAL":ext_names,"A2_PM":pm_names,
                        "A3_EXTERNAL_PM":sorted(set(ext_names)|set(pm_names)),
                        "A4_RESIDUAL":["pm_minus_external_fair_residual"],
                        "A5_FULL_EXECUTION":full_names},
        "external_fair_level_model_features":external_level_names,
        "model":"RIDGE_FUTURE_PM_REPRICING;SIGN_DRIVES_TOXIC_SIDE_VETO",
        "threshold":"ZERO_PREDICTED_PM_DELTA_FIXED_NO_OOS_TUNING",
    }
    atomic_json(output/"00_manifest.json",manifest)
    atomic_json(output/"01_grid.json",{"schema":SCHEMA+"_grid",**SAFETY_PLUS,"policies":grid})
    atomic_json(output/"02_training_receipts.json",{"schema":SCHEMA+"_training",**SAFETY_PLUS,"cells":receipts})
    mono=monotonicity(grid)
    atomic_json(output/"03_monotonicity.json",{"schema":SCHEMA+"_monotonicity",**SAFETY_PLUS,"policies":mono})

    fields=[
        "policy","latency_ms","holding_horizon_ms","observations","selected_quote_episodes",
        "execution_observed","economic_observed","fill_episodes","filled_shares","fill_probability",
        "direction_accuracy","mean_future_pm_move","gross_spread_capture","gross_markout",
        "executable_markout","adverse_markout","net_pnl","net_pnl_per_posted_share",
        "net_pnl_per_fill_share","toxic_fill_probability","mean_inventory_seconds_per_filled_share",
        "turnover_filled_per_posted",
    ]
    with (output/"04_grid.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for p in POLICIES:
            for l in LATENCIES:
                for h in EXITS:
                    cell=grid[p].get(f"{l}::{h}")
                    if cell is None:continue
                    w.writerow({"policy":p,"latency_ms":l,"holding_horizon_ms":h,
                                **{k:cell.get(k) for k in fields[3:]}})

    summary={}
    for p in POLICIES:
        cells=[(k,v) for k,v in grid[p].items() if finite(v.get("net_pnl_per_posted_share"))]
        best=max(cells,key=lambda kv:float(kv[1]["net_pnl_per_posted_share"])) if cells else None
        summary[p]={
            "best_cell":None if best is None else best[0],
            "best_net_pnl_per_posted_share":None if best is None else best[1]["net_pnl_per_posted_share"],
            "best_net_pnl_per_fill_share":None if best is None else best[1]["net_pnl_per_fill_share"],
            "best_fill_probability":None if best is None else best[1]["fill_probability"],
            "best_toxic_fill_probability":None if best is None else best[1]["toxic_fill_probability"],
            "best_direction_accuracy":None if best is None else best[1]["direction_accuracy"],
        }
    atomic_json(output/"05_summary.json",{"schema":SCHEMA+"_summary",**SAFETY_PLUS,
                                          "selection_guardrail":"BEST_CELL_DESCRIPTIVE_ONLY_ON_LOCKED_OOS_NOT_FOR_PROMOTION",
                                          "policies":summary})
    print("MAKER_A0_A5_READY="+json.dumps(summary,sort_keys=True,separators=(",",":")))
    return {"state":"READY","summary":summary,"rows":len(rows),"train":len(train),"oos":len(oos)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    p.add_argument("--code-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    try:result=run(a.root,a.output_dir,a.code_sha,a.minimum_wall_ns)
    except (OSError,ValueError,RuntimeError,np.linalg.LinAlgError) as exc:
        p.exit(2,type(exc).__name__+":"+str(exc)+"\n")
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
