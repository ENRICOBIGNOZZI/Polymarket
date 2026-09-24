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
    EXITS, LATENCIES, SIZE, jsonl_sessions, pair_asof_session, side_state,
    stream_sessions, stream_raw_sessions,
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
ANCHOR_CADENCE_MS=500

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



def _flatten(prefix: str, value: Any, out: dict[str,float]) -> None:
    if isinstance(value,dict):
        for key,child in value.items():
            _flatten(prefix+"."+str(key) if prefix else str(key),child,out)
    elif finite(value):
        out[prefix]=float(value)


def build_static_market_metadata(decisions: list[dict[str,Any]]):
    """Use native decisions only for static venue terms, never for quote timing."""
    by_market=defaultdict(list)
    by_context=defaultdict(list)
    for row in decisions:
        try:
            rate=float(row["fee_rate"]); exponent=float(row["fee_exponent"])
            minimum=float(row["minimum"])
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
        if not all(math.isfinite(x) for x in (rate,exponent,minimum)):
            continue
        if rate<0 or exponent<0 or minimum<0:
            continue
        value=(round(rate,12),round(exponent,12),round(minimum,9))
        market=str(row.get("market_id") or "")
        context=(str(row.get("asset") or ""),str(row.get("horizon") or ""))
        if market:by_market[market].append(value)
        if all(context):by_context[context].append(value)

    def summarize(values):
        if not values:return None
        fee_pairs={(x[0],x[1]) for x in values}
        if len(fee_pairs)!=1:return None
        rate,exponent=next(iter(fee_pairs))
        # Maximum observed minimum is conservative and does not use PnL.
        return {"fee_rate":rate,"fee_exponent":exponent,
                "minimum":max(x[2] for x in values)}

    return (
        {k:v for k,values in by_market.items() if (v:=summarize(values)) is not None},
        {k:v for k,values in by_context.items() if (v:=summarize(values)) is not None},
    )


def load_market_metadata(path: Path) -> dict[str,dict[str,Any]]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value,dict)
        or value.get("schema")!="polymarket_v7_maker_market_metadata_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
    ):
        raise ValueError("MARKET_METADATA_CONTRACT_INVALID")
    out={}
    for row in value.get("markets") or []:
        if not isinstance(row,dict):continue
        market=str(row.get("market_id") or "")
        yes=str(row.get("yes_token") or "");no=str(row.get("no_token") or "")
        asset=str(row.get("asset") or "").upper();horizon=str(row.get("horizon") or "").upper()
        try:
            start=int(row.get("start_timestamp_ms") or 0)
            end=int(row.get("end_timestamp_ms") or 0)
        except (TypeError,ValueError,OverflowError):
            continue
        if not market or not yes or not no or yes==no or not asset or not horizon or start<=0 or end<=start:
            continue
        out[market]=dict(row)
    if not out:
        raise ValueError("MARKET_METADATA_EMPTY")
    return out


def _l5_imbalance(raw: dict[str,Any]) -> float|None:
    bids=raw.get("bid_levels_l10") if isinstance(raw.get("bid_levels_l10"),list) else []
    asks=raw.get("ask_levels_l10") if isinstance(raw.get("ask_levels_l10"),list) else []
    def total(rows):
        value=0.0
        for row in rows[:5]:
            if not isinstance(row,dict):continue
            size=row.get("size")
            if finite(size) and float(size)>0:value+=float(size)
        return value
    bid=total(bids);ask=total(asks)
    return (bid-ask)/(bid+ask) if bid+ask>1e-12 else None


def oriented_pm_anchor_features(raw: dict[str,Any], *, outcome: str) -> dict[str,float]:
    placement=raw.get("placement_features") if isinstance(raw.get("placement_features"),dict) else {}
    direction=1.0 if outcome=="YES" else -1.0
    out={}
    directional=("imbalance","ofi","short_return_ticks","microstructure_shadow_delta_250ms")
    level=("spread_ticks","ew_vol_ticks","trade_intensity","cancel_intensity","local_latency_ms")
    for name in directional:
        value=placement.get(name)
        if finite(value):out["pm.anchor_"+name]=direction*float(value)
    for name in level:
        value=placement.get(name)
        if finite(value):out["pm.anchor_"+name]=float(value)
    buy=placement.get("aggressive_buy_prints_per_second")
    sell=placement.get("aggressive_sell_prints_per_second")
    if finite(buy) and finite(sell):
        out["pm.anchor_aggressive_net_prints_per_second"]=direction*(float(buy)-float(sell))
        out["pm.anchor_aggressive_total_prints_per_second"]=float(buy)+float(sell)
    bid=raw.get("bid_depth_l1");ask=raw.get("ask_depth_l1")
    if finite(bid) and finite(ask) and float(bid)+float(ask)>1e-12:
        out["pm.anchor_l1_depth_imbalance"]=direction*((float(bid)-float(ask))/(float(bid)+float(ask)))
    l5=_l5_imbalance(raw)
    if l5 is not None:
        out["pm.anchor_l5_depth_imbalance"]=direction*l5
    return out


def load_book_anchor_rows(
    root: Path, *, minimum_wall_ns: int,
    metadata: dict[str,dict[str,Any]],
    market_meta: dict[str,dict[str,float]],
    context_meta: dict[tuple[str,str],dict[str,float]],
):
    """Exact 500ms alpha-neutral quote clock with causal PM-book as-of state.

    Quote timestamps are fixed grid points. Book events only provide the latest
    information available at or before each grid point; they never choose the
    quote timestamp. This removes both old-alpha selection and within-slot
    book-activity timing selection.
    """
    book_root=root/"research"/"repricing_book"/"book_observations"
    if not book_root.is_dir():
        raise ValueError("PM_BOOK_OBSERVATION_DIR_MISSING")
    paths=sorted(
        (p for p in book_root.glob("*.jsonl*") if p.is_file() and not p.is_symlink()),
        key=lambda p:(p.name=="current.jsonl",p.name),
    )
    events=defaultdict(list)
    counts=Counter()
    model_shas=set()
    cadence_ns=ANCHOR_CADENCE_MS*1_000_000
    for path in paths:
        counts["files"]+=1
        for raw in robust_json_lines(path):
            if raw.get("schema")!="polymarket_v7_causal_book_observation_v1":
                continue
            counts["schema_rows"]+=1
            if (
                raw.get("paper_only") is not True
                or raw.get("authenticated_execution") is not False
                or raw.get("real_order_submission") is not False
                or raw.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY"
                or raw.get("valid") is not True
                or raw.get("lineage_continuous") is not True
            ):
                counts["authority_or_continuity_rejected"]+=1;continue
            sha=str(raw.get("model_sha") or "")
            if len(sha)==40:model_shas.add(sha)
            try:
                observed_ns=int(raw["receive_wall_ms"])*1_000_000
            except (KeyError,TypeError,ValueError,OverflowError):
                counts["clock_rejected"]+=1;continue
            if observed_ns<minimum_wall_ns:
                counts["before_minimum"]+=1;continue
            market=str(raw.get("market_id") or "")
            token=str(raw.get("token_id") or "")
            meta=metadata.get(market)
            if meta is None:
                counts["metadata_unavailable"]+=1;continue
            start=int(meta["start_timestamp_ms"])*1_000_000
            end=int(meta["end_timestamp_ms"])*1_000_000
            if not start<=observed_ns<end:
                counts["outside_contract_window"]+=1;continue
            yes=str(meta["yes_token"]);no=str(meta["no_token"])
            if token==yes:outcome="YES"
            elif token==no:outcome="NO"
            else:
                counts["token_mismatch"]+=1;continue
            asset=str(meta["asset"]).upper();horizon=str(meta["horizon"]).upper()
            static=market_meta.get(market) or context_meta.get((asset,horizon)) or {}
            rate=meta.get("fee_rate")
            exponent=meta.get("fee_exponent")
            minimum=meta.get("minimum")
            if not finite(rate):rate=static.get("fee_rate")
            if not finite(exponent):exponent=static.get("fee_exponent")
            if not finite(minimum):minimum=static.get("minimum")
            if not all(finite(x) for x in (rate,exponent,minimum)):
                counts["static_terms_unavailable"]+=1;continue
            events[market].append({
                "observed_ns":observed_ns,"raw":raw,"sha":sha,"outcome":outcome,
                "asset":asset,"horizon":horizon,"yes":yes,"no":no,
                "start_ns":start,"end_ns":end,
                "fee_rate":float(rate),"fee_exponent":float(exponent),
                "minimum":float(minimum),
            })
            counts["eligible_events"]+=1

    rows=[]
    for market,seq in sorted(events.items()):
        seq.sort(key=lambda e:(
            int(e["observed_ns"]),
            int(e["raw"].get("observer_sequence") or 0),
            str(e["raw"].get("token_id") or ""),
        ))
        stamps=[int(e["observed_ns"]) for e in seq]
        first=max(int(minimum_wall_ns),int(seq[0]["start_ns"]),stamps[0])
        last=min(int(seq[-1]["end_ns"])-1,stamps[-1])
        slot=((first+cadence_ns-1)//cadence_ns)*cadence_ns
        stop=(last//cadence_ns)*cadence_ns
        while slot<=stop:
            pos=bisect_right(stamps,slot)-1
            if pos<0:
                slot+=cadence_ns;continue
            event=seq[pos]
            raw=event["raw"]
            if not int(event["start_ns"])<=slot<int(event["end_ns"]):
                slot+=cadence_ns;continue
            age_ns=slot-int(event["observed_ns"])
            if age_ns<0:
                raise RuntimeError("PM_ANCHOR_ASOF_FUTURE_LEAK")
            features=oriented_pm_anchor_features(raw,outcome=str(event["outcome"]))
            features["execution.tte_seconds"]=(int(event["end_ns"])-slot)/1e9
            features["pm.anchor_age_ms"]=age_ns/1e6
            decision_id=canonical_hash([
                "maker-book-anchor-v2",str(event["sha"]),market,slot,
                str(raw.get("observer_session_id") or ""),
                int(raw.get("observer_sequence") or 0),
            ])
            rows.append({
                "decision_id":decision_id,
                "market_id":market,"asset":event["asset"],"horizon":event["horizon"],
                "decision_ns":slot,"information_end_ns":int(event["observed_ns"]),
                "yes_token_id":event["yes"],"no_token_id":event["no"],"token_id":event["yes"],
                "tte_ns":int(event["end_ns"])-slot,"signal_age_ns":age_ns,"direction":0,
                "fee_rate":event["fee_rate"],"fee_exponent":event["fee_exponent"],
                "minimum":event["minimum"],"epoch":int(raw.get("connection_epoch") or 0),
                "pair":{},"features":features,
                "anchor_outcome":event["outcome"],
                "anchor_observer_sequence":int(raw.get("observer_sequence") or 0),
                "anchor_source":"CAUSAL_PM_BOOK_EXACT_FIXED_CADENCE_ASOF",
            })
            counts["grid_slots"]+=1
            slot+=cadence_ns

    rows.sort(key=lambda r:(int(r["decision_ns"]),str(r["market_id"]),str(r["decision_id"])))
    counts["anchors"]=len(rows)
    if not rows:
        raise ValueError("NO_ALPHA_NEUTRAL_BOOK_ANCHORS")
    return rows,{
        "counts":dict(counts),"model_shas":sorted(model_shas),
        "anchor_cadence_ms":ANCHOR_CADENCE_MS,
        "timing_selection":"EXACT_500MS_GRID_PER_ACTIVE_MARKET;LATEST_VALID_PM_EVENT_ASOF_GRID;NO_ALPHA_NO_PNL_NO_WITHIN_SLOT_EVENT_TIMING",
        "feature_semantics":"PM_EVENT_FEATURES_ORIENTED_TO_YES_PROBABILITY;FEATURE_AGE_EXPLICIT",
    }


def load_feature_anchor_rows(
    paths: Iterable[Path], *, minimum_wall_ns: int,
    market_meta: dict[str,dict[str,float]],
    context_meta: dict[tuple[str,str],dict[str,float]],
):
    """Create alpha-neutral maker quote opportunities from the continuous feature tape."""
    chosen={}
    counts=Counter()
    model_shas=set()
    cadence_ns=ANCHOR_CADENCE_MS*1_000_000
    for path in sorted({Path(p).resolve() for p in paths}):
        counts["files"]+=1
        for raw in robust_json_lines(path):
            if raw.get("schema")!="polymarket_v7_multi_crypto_feature_tape_v1":
                continue
            counts["schema_rows"]+=1
            if not (
                raw.get("paper_only") is True
                and raw.get("authenticated_execution") is False
                and raw.get("real_order_submission") is False
                and raw.get("execution_authority") is False
            ):
                counts["authority_rejected"]+=1;continue
            sha=str(raw.get("model_sha") or "")
            if len(sha)!=40 or any(ch not in "0123456789abcdef" for ch in sha):
                counts["model_sha_rejected"]+=1;continue
            model_shas.add(sha)
            try:
                decision=int(raw["decision_wall_ns"])
                available=int(raw["available_at_ns"])
            except (KeyError,TypeError,ValueError,OverflowError):
                counts["clock_rejected"]+=1;continue
            if decision<minimum_wall_ns:
                counts["before_minimum"]+=1;continue
            if available<=0 or available>decision:
                counts["future_availability_rejected"]+=1;continue
            if raw.get("active_now") is not True:
                counts["inactive"]+=1;continue
            market=str(raw.get("market_id") or "")
            asset=str(raw.get("asset") or "").upper()
            horizon=str(raw.get("horizon") or "").upper()
            yes=str(raw.get("yes_token") or "")
            no=str(raw.get("no_token") or "")
            if not market or not asset or not horizon or not yes or not no or yes==no:
                counts["identity_rejected"]+=1;continue
            meta=market_meta.get(market) or context_meta.get((asset,horizon))
            if meta is None:
                counts["static_terms_unavailable"]+=1;continue
            features={}
            _flatten("tape",raw.get("features") or {},features)
            tte=features.get("tape.tte_seconds")
            if tte is None or tte<=0:
                counts["tte_rejected"]+=1;continue
            bucket=decision//cadence_ns
            key=(market,bucket)
            decision_id=canonical_hash([
                "maker-feature-anchor-v1",sha,market,decision,bucket,
                str(raw.get("source_identity_hash") or ""),
            ])
            row={
                "decision_id":decision_id,
                "market_id":market,"asset":asset,"horizon":horizon,
                "decision_ns":decision,"information_end_ns":decision,
                "yes_token_id":yes,"no_token_id":no,
                "token_id":yes,"tte_ns":int(float(tte)*1e9),
                "signal_age_ns":0,"direction":0,
                "fee_rate":float(meta["fee_rate"]),
                "fee_exponent":float(meta["fee_exponent"]),
                "minimum":float(meta["minimum"]),
                "epoch":0,"pair":{},
                "features":features,
                "feature_available_at_ns":available,
                "feature_model_sha":sha,
                "feature_source_identity_hash":raw.get("source_identity_hash"),
            }
            prior=chosen.get(key)
            # First causal snapshot in each fixed 500ms quote slot wins. This is
            # determined before outcomes and matches the frozen TTL.
            if prior is None or decision<int(prior["decision_ns"]):
                chosen[key]=row
            counts["eligible_records"]+=1
    if len(model_shas)>1:
        raise ValueError("MULTIPLE_FEATURE_RUNTIME_SHAS")
    rows=sorted(chosen.values(),key=lambda r:(int(r["decision_ns"]),str(r["market_id"]),str(r["decision_id"])))
    counts["anchors"]=len(rows)
    return rows,{"counts":dict(counts),"feature_runtime_shas":sorted(model_shas),
                 "anchor_cadence_ms":ANCHOR_CADENCE_MS,
                 "timing_selection":"FIRST_CAUSAL_FEATURE_SNAPSHOT_PER_MARKET_PER_FIXED_SLOT_NO_PNL"}


VENUE_ID_TO_NAME={1:"binance",2:"coinbase",3:"bybit"}
EXTERNAL_RETURN_WINDOWS_MS=(50,100,250,1000)
MAX_EXTERNAL_ASOF_AGE_NS=1_000_000_000


def load_external_venue_csvs(specs: Iterable[str]):
    """Load normalized venue events using London local receive wall time only."""
    series=defaultdict(list)
    counts=Counter()
    for spec in specs:
        if "=" not in str(spec):
            raise ValueError("EXTERNAL_VENUE_CSV_SPEC_INVALID")
        asset,raw_path=str(spec).split("=",1)
        asset=asset.strip().upper()
        path=Path(raw_path)
        if not asset or not path.is_file():
            raise ValueError("EXTERNAL_VENUE_CSV_MISSING:"+str(spec))
        counts["files"]+=1
        with path.open("r",encoding="utf-8") as handle:
            for line in handle:
                parts=line.strip().split(",")
                if len(parts)!=5:
                    counts["invalid_rows"]+=1;continue
                try:
                    wall=int(parts[0]);venue_id=int(parts[1]);event_type=int(parts[2])
                    epoch=int(parts[3]);price=float(parts[4])
                except (TypeError,ValueError,OverflowError):
                    counts["invalid_rows"]+=1;continue
                venue=VENUE_ID_TO_NAME.get(venue_id)
                if venue is None or wall<=0 or epoch<=0 or event_type not in (1,2) or not math.isfinite(price) or price<=0:
                    counts["invalid_rows"]+=1;continue
                series[(asset,venue)].append((wall,epoch,price))
                counts["accepted"]+=1
    indexed={}
    for key,seq in series.items():
        seq.sort(key=lambda x:(x[0],x[1],x[2]))
        indexed[key]={"rows":seq,"stamps":[x[0] for x in seq]}
    counts["asset_venue_series"]=len(indexed)
    diagnostics=dict(counts)
    diagnostics["series"]=[asset+":"+venue for asset,venue in sorted(indexed)]
    return indexed,diagnostics


def _venue_asof(index, asset: str, venue: str, target_ns: int):
    item=index.get((asset,venue))
    if item is None:return None
    pos=bisect_right(item["stamps"],int(target_ns))-1
    if pos<0:return None
    wall,epoch,price=item["rows"][pos]
    if target_ns-wall<0 or target_ns-wall>MAX_EXTERNAL_ASOF_AGE_NS:
        return None
    return wall,epoch,price


def external_venue_features(index, asset: str, decision_ns: int) -> dict[str,float]:
    out={}
    current_prices=[]
    ret100=[]
    for venue in ("binance","coinbase","bybit"):
        current=_venue_asof(index,asset,venue,decision_ns)
        if current is None:
            continue
        current_wall,current_epoch,current_price=current
        out[f"external.{venue}_age_ms"]=(decision_ns-current_wall)/1e6
        current_prices.append((venue,current_price))
        for window in EXTERNAL_RETURN_WINDOWS_MS:
            target=decision_ns-window*1_000_000
            prior=_venue_asof(index,asset,venue,target)
            if prior is None or prior[1]!=current_epoch or prior[2]<=0:
                continue
            value=10_000.0*math.log(current_price/prior[2])
            suffix="1s" if window==1000 else f"{window}ms"
            out[f"external.{venue}_return_{suffix}_bp"]=value
            if window==100:
                ret100.append(value)
    if current_prices:
        prices=sorted(price for _,price in current_prices)
        median=prices[len(prices)//2] if len(prices)%2 else 0.5*(prices[len(prices)//2-1]+prices[len(prices)//2])
        mean=statistics.fmean(prices)
        out["external.fresh_venue_count_receive_time"]=float(len(prices))
        if mean>0:
            out["external.cross_venue_dispersion_bps_receive_time"]=10_000.0*(
                math.sqrt(statistics.fmean((p-mean)*(p-mean) for p in prices))/mean)
        if median>0:
            for venue,price in current_prices:
                out[f"external.{venue}_basis_to_median_bps"]=10_000.0*(price/median-1.0)
    if ret100:
        signs=[1 if x>1e-15 else -1 if x<-1e-15 else 0 for x in ret100]
        out["external.cross_venue_consensus_sign_100ms"]=statistics.fmean(signs)
        out["external.cross_venue_agreement_100ms"]=abs(sum(signs))/len(signs)
        leader=max(ret100,key=abs)
        out["external.cross_venue_leader_return_100ms_bp"]=float(leader)
    return out


def attach_external_venue_features(rows: list[dict[str,Any]], index):
    counts=Counter()
    by_asset=defaultdict(Counter)
    output=[]
    for original in rows:
        row=dict(original)
        row["features"]=dict(original.get("features") or {})
        asset=str(row.get("asset") or "").upper()
        added=external_venue_features(index,asset,int(row["decision_ns"]))
        row["features"].update(added)
        output.append(row)
        if added:
            counts["joined"]+=1
        else:
            counts["missing"]+=1
        for venue in ("binance","coinbase","bybit"):
            key=f"external.{venue}_return_100ms_bp"
            by_asset[asset][venue+"_100ms_ready"]+=int(key in added)
        by_asset[asset]["rows"]+=1
    counts["rows"]=len(rows)
    return output,{"counts":dict(counts),"by_asset":{a:dict(v) for a,v in sorted(by_asset.items())},
                   "semantics":"BACKWARD_ASOF_LOCAL_RECEIVE_WALL_NS_ONLY;NO_EXCHANGE_TIME_SUBSTITUTION"}


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
        "signal_return","signal_age","parent_shock","shock",
        "binance_return","coinbase_return","bybit_return",
        "external.return_","tape.external.return_",
        "dispersion","fresh_venue","agreement","venue_leader","venue_laggard",
        "composite_price","external.aggregate_ofi","external.aggregate_trade_imbalance",
        "tape.external.aggregate_ofi","tape.external.aggregate_trade_imbalance",
        "leader_features",
    ))


def external_fair_feature(name: str) -> bool:
    """External-only level/state features for p_external; never PM/CLOB state."""
    n=name.lower()
    if not safe_feature_name(n):
        return False
    if any(x in n for x in (
        "pm.","pm_","polymarket","clob","book_","book.","queue","fill",
        "markout","tox","adverse_selection","spread_ticks","imbalance",
        "microprice",
    )):
        return False
    if external_feature(n):
        return True
    return any(x in n for x in (
        "oracle","reference","strike","spot","index_price","mark_price",
        "perp","basis","funding","open_interest","liquidat","deribit",
        "implied_vol","realized_vol","native_vol","vol_fast","vol_slow",
        "distance_to_","distance_from_","time_to_resolution","tte",
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


def base_context(
    row: dict[str,Any], *, include_pm: bool, include_signal_age: bool,
) -> dict[str,float]:
    out={"ctx.tte_s":float(row.get("tte_ns") or 0)/1e9}
    if include_pm:
        pm_yes=(row.get("pair") or {}).get("pm_yes")
        if finite(pm_yes):
            out["ctx.pm_yes"]=float(pm_yes)
    if include_signal_age:
        out["ctx.signal_age_ms"]=float(row.get("signal_age_ns") or 0)/1e6
    out["asset::"+str(row.get("asset") or "UNKNOWN")]=1.0
    out["contract::"+str(row.get("horizon") or "UNKNOWN")]=1.0
    return out


def feature_dict(
    row: dict[str,Any], names: list[str], *,
    include_pm: bool, include_signal_age: bool,
) -> dict[str,float|None]:
    f=row.get("features") or {}
    out={name:(float(f[name]) if name in f and finite(f[name]) else None) for name in names}
    out.update(base_context(
        row, include_pm=include_pm, include_signal_age=include_signal_age))
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
    """Directional target is p(t+h)-p(t); latency is an execution stress only."""
    del latency_ms
    now=current_pair(row,session)
    if now is None:return None
    target=int(row["decision_ns"])/1e6+int(horizon_ms)
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
    token=str((row.get("yes_token_id") if side=="YES" else row.get("no_token_id")) or "")
    if not token:return None,"TOKEN_ID_UNAVAILABLE"
    price=float(state["bid"])
    tick=float((pair.get("yes_tick_size") if side=="YES" else pair.get("no_tick_size")) or row.get("tick") or 0.01)
    if TARGET_SHARES+1e-12<float(row.get("minimum") or 0.0):
        return None,"VENUE_MINIMUM_ABOVE_TARGET_SIZE"
    ahead=max(0.0,float(state["bid_depth"])*QUEUE_AHEAD_MULTIPLIER)
    idx=trades.get((str(row["market_id"]),token))
    end_ms=arrival_ms+QUOTE_TTL_MS
    if idx is None:
        return {"fills":[],"quote_price":price,"arrival_pair":pair,"posted_shares":TARGET_SHARES},"OBSERVED_NO_TRADES"
    left=bisect_right(idx["stamps"],arrival_ms)
    # Trade exactly at expiry has ambiguous order at millisecond resolution.
    right=bisect_left(idx["stamps"],end_ms)
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



def recent_trade_flow(row: dict[str,Any], trades, window_ms: int=1000) -> dict[str,float]:
    """Receive-time public flow strictly before the quote decision."""
    decision_ms=int(row["decision_ns"])//1_000_000
    out={}
    for outcome,token in (
        ("yes",str(row.get("yes_token_id") or "")),
        ("no",str(row.get("no_token_id") or "")),
    ):
        idx=trades.get((str(row["market_id"]),token))
        buy=sell=buy_n=sell_n=0.0
        if idx is not None:
            left=bisect_left(idx["stamps"],decision_ms-window_ms)
            # Strictly before the decision. Same-millisecond receive records
            # have unknown ordering relative to the anchor and are therefore excluded.
            right=bisect_left(idx["stamps"],decision_ms)
            for wall,epoch,aggressor,price,size in idx["rows"][left:right]:
                if aggressor=="BUY":
                    buy+=float(size);buy_n+=1.0
                elif aggressor=="SELL":
                    sell+=float(size);sell_n+=1.0
        out[f"pm.{outcome}_aggressive_buy_shares_1s"]=buy
        out[f"pm.{outcome}_aggressive_sell_shares_1s"]=sell
        out[f"pm.{outcome}_aggressive_net_shares_1s"]=buy-sell
        out[f"pm.{outcome}_aggressive_buy_prints_1s"]=buy_n
        out[f"pm.{outcome}_aggressive_sell_prints_1s"]=sell_n
    return out


def pm_execution_context(
    row: dict[str,Any], session, trades, *, include_queue: bool,
) -> dict[str,float|None]:
    pair=current_pair(row,session)
    if pair is None:return {}
    yes=side_state(pair,"YES");no=side_state(pair,"NO")
    out={
        "pm.yes_bid_depth_l1":None if yes is None else yes["bid_depth"],
        "pm.yes_ask_depth_l1":None if yes is None else yes["ask_depth"],
        "pm.no_bid_depth_l1":None if no is None else no["bid_depth"],
        "pm.no_ask_depth_l1":None if no is None else no["ask_depth"],
        "pm.yes_spread":None if yes is None else yes["ask"]-yes["bid"],
        "pm.no_spread":None if no is None else no["ask"]-no["bid"],
    }
    out.update(recent_trade_flow(row,trades))
    if include_queue:
        out.update({
            "execution.yes_queue_ahead":None if yes is None else QUEUE_AHEAD_MULTIPLIER*yes["bid_depth"],
            "execution.no_queue_ahead":None if no is None else QUEUE_AHEAD_MULTIPLIER*no["bid_depth"],
            "execution.quote_lifetime_ms":float(QUOTE_TTL_MS),
            "execution.queue_multiplier":float(QUEUE_AHEAD_MULTIPLIER),
        })
    return out


def prediction_names(train_rows):
    ext=select_names(train_rows,external_feature)
    pm=select_names(train_rows,pm_feature)
    full=select_names(train_rows,full_execution_feature)
    fair=select_names(train_rows,external_fair_feature)
    return ext,pm,full,fair


def train_external_fair(train_rows,session_cache,ext_names):
    records=[];targets=[]
    for row in train_rows:
        session,_=session_cache[str(row["decision_id"])]
        if session is None:continue
        pair=current_pair(row,session)
        if pair is None:continue
        records.append(feature_dict(row,ext_names,include_pm=False,include_signal_age=False))
        targets.append(float(pair["pm_yes"]))
    names=sorted(set(ext_names)|{k for r in records for k in r if k.startswith(("asset::","contract::","ctx."))})
    model=Ridge(names).fit([{k:r.get(k) for k in names} for r in records],targets)
    return model,names


def residual_features(row,session,external_model,external_names):
    """A4 sees PM only through the signed PM-minus-external residual."""
    pair=current_pair(row,session)
    if pair is None:return None
    base=feature_dict(row,external_names,include_pm=False,include_signal_age=False)
    fair=min(1.0,max(0.0,external_model.predict(base)))
    residual=float(pair["pm_yes"])-fair
    tte_s=float(row.get("tte_ns") or 0)/1e9
    return {
        "residual.pm_minus_external":residual,
        "residual.abs":abs(residual),
        "residual.x_tte_s":residual*tte_s,
        "ctx.tte_s":tte_s,
        "asset::"+str(row.get("asset") or "UNKNOWN"):1.0,
        "contract::"+str(row.get("horizon") or "UNKNOWN"):1.0,
    }


def fit_cell_models(train_rows,session_cache,latency,horizon,ext_names,pm_names,full_names,fair_names,external_model,trades):
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
            rec=feature_dict(row,names0,include_pm=(policy!="A1_EXTERNAL"),include_signal_age=(policy!="A2_PM"))
            if policy in ("A2_PM","A3_EXTERNAL_PM","A5_FULL_EXECUTION"):
                rec.update(pm_execution_context(
                    row,session,trades,include_queue=(policy=="A5_FULL_EXECUTION")))
            if policy=="A5_FULL_EXECUTION":
                rec.update({
                    "action.latency_ms":float(latency),
                    "action.horizon_ms":float(horizon),
                    "action.quote_ttl_ms":float(QUOTE_TTL_MS),
                    "action.queue_multiplier":float(QUEUE_AHEAD_MULTIPLIER),
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
        rec=residual_features(row,session,external_model,fair_names)
        if rec is None:continue
        records.append(rec);targets.append(float(y))
    names=sorted({k for rec in records for k in rec})
    try:
        models["A4_RESIDUAL"]=Ridge(names).fit([{k:r.get(k) for k in names} for r in records],targets)
        receipts["A4_RESIDUAL"]={"state":"READY","training_targets":len(targets),"features":names}
    except (ValueError,np.linalg.LinAlgError) as exc:
        receipts["A4_RESIDUAL"]={"state":"INSUFFICIENT_DATA","reason":str(exc),"training_targets":len(targets),"features":names}
    return models,receipts


def predict_policy(policy,row,session,model,ext_names,pm_names,full_names,fair_names,external_model,latency,horizon,trades):
    if policy=="A0_BASELINE":return None
    if policy=="A1_EXTERNAL":rec=feature_dict(row,ext_names,include_pm=False,include_signal_age=False)
    elif policy=="A2_PM":rec=feature_dict(row,pm_names,include_pm=True,include_signal_age=False)
    elif policy=="A3_EXTERNAL_PM":rec=feature_dict(row,sorted(set(ext_names)|set(pm_names)),include_pm=True,include_signal_age=True)
    elif policy=="A4_RESIDUAL":
        rec=residual_features(row,session,external_model,fair_names)
        if rec is None:return None
    elif policy=="A5_FULL_EXECUTION":
        rec=feature_dict(row,full_names,include_pm=True,include_signal_age=True)
    else:return None
    if policy in ("A2_PM","A3_EXTERNAL_PM","A5_FULL_EXECUTION"):
        rec.update(pm_execution_context(
            row,session,trades,include_queue=(policy=="A5_FULL_EXECUTION")))
    if policy=="A5_FULL_EXECUTION":
        rec.update({
            "action.latency_ms":float(latency),"action.horizon_ms":float(horizon),
            "action.quote_ttl_ms":float(QUOTE_TTL_MS),"action.queue_multiplier":float(QUEUE_AHEAD_MULTIPLIER),
        })
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


def evaluate_cell(oos_rows,session_cache,trades,models,latency,horizon,ext_names,pm_names,full_names,fair_names,external_model):
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
                p,row,session,model,ext_names,pm_names,full_names,fair_names,external_model,latency,horizon,trades)
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
                token=str((row.get("yes_token_id") if side=="YES" else row.get("no_token_id")) or "")
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


def run(
    root: Path, output: Path, code_sha: str, minimum_wall_ns: int,
    external_venue_csv_specs: Iterable[str]=(),
    market_metadata_path: Path|None=None,
):
    # Native lead-lag decisions are used only as an authoritative source for
    # static fee/minimum terms. Quote timing comes exclusively from the
    # continuous alpha-neutral feature tape below.
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        raise ValueError("CAUSAL_DATASET_NOT_READY:"+str(data.get("input_state")))
    native_terms=[r for r in data["decisions"] if da._valid_state(r)]
    market_meta,context_meta=build_static_market_metadata(native_terms)
    if market_metadata_path is None:
        raise ValueError("MARKET_METADATA_REQUIRED")
    metadata=load_market_metadata(market_metadata_path)
    source,feature_diag=load_book_anchor_rows(
        root,minimum_wall_ns=minimum_wall_ns,metadata=metadata,
        market_meta=market_meta,context_meta=context_meta)

    external_venue_index,external_venue_load_diag=load_external_venue_csvs(external_venue_csv_specs)
    if not external_venue_index:
        raise ValueError("NO_RECEIVE_TIME_EXTERNAL_VENUE_TAPE")
    source,external_venue_join_diag=attach_external_venue_features(source,external_venue_index)

    sessions,tape_diag=stream_sessions(root.resolve().parent,source)
    if not sessions:
        sessions,fallback=jsonl_sessions(root.resolve(),source)
        tape_diag={**tape_diag,**fallback,"fallback":"JSONL_BOOK_OBSERVATIONS"}
    if not sessions:
        sessions,raw_diag=stream_raw_sessions(root.resolve(),source)
        tape_diag={**tape_diag,**raw_diag,"fallback":"RAW_CAUSAL_BOOK_TOKEN_INDEX"}
    if not sessions:
        raise ValueError("NO_CAUSAL_PM_BOOK_SESSIONS")
    market_index=build_market_session_index(sessions)
    session_cache={}
    usable=[]
    for original in source:
        row=dict(original)
        session,reason=resolve_session(row,market_index)
        if session is None:
            session_cache[str(row["decision_id"])]=(None,reason)
            continue
        pair=current_pair(row,session)
        if pair is None:
            session_cache[str(row["decision_id"])]=(None,"PM_PAIR_AT_ANCHOR_UNAVAILABLE")
            continue
        row["pair"]=pair
        row["epoch"]=int(pair.get("connection_epoch") or 0)
        row["tick"]=max(float(pair.get("yes_tick_size") or 0.0),
                         float(pair.get("no_tick_size") or 0.0))
        session_cache[str(row["decision_id"])]=(session,None)
        usable.append(row)
    if not usable:
        raise ValueError("NO_CONTINUOUS_PM_FEATURE_ANCHORS")

    window=select_two_hour_window(usable,session_cache)
    start_ns,end_ns=int(window["start_ns"]),int(window["end_ns"])
    rows=[r for r in usable if start_ns<=int(r["decision_ns"])<end_ns]
    feature_paths=discover_feature_tapes(root)
    if not feature_paths:
        raise ValueError("NO_RICH_FEATURE_TAPE")
    feature_index,rich_load_diag=load_feature_tape(
        feature_paths,start_ns=start_ns,end_ns=end_ns)
    rows,join_diag=attach_rich_state(rows,feature_index,delay_ms=0)
    if not int(join_diag.get("joined_rows") or 0):
        raise ValueError("NO_RICH_FEATURE_ROWS_AT_FIXED_ANCHORS")
    join_diag={**join_diag,
        "semantics":"EXACT_FIXED_500MS_QUOTE_CLOCK;RICH_STATE_BACKWARD_ASOF_AVAILABLE_AT_NS_LE_DECISION;NO_TAKER_SIGNAL_TIMING",
    }
    train,oos,cut=split_60_40(rows,start_ns)
    if not train or not oos:
        raise ValueError("EMPTY_60_40_SPLIT")
    ext_names,pm_names,full_names,fair_names=prediction_names(train)
    if not ext_names:raise ValueError("NO_EXTERNAL_FEATURES")
    if not pm_names:raise ValueError("NO_PM_FEATURES")
    if not fair_names:raise ValueError("NO_EXTERNAL_FAIR_FEATURES")
    rich_a5_names=[name for name in full_names if str(name).startswith("tape.")]
    if not rich_a5_names:raise ValueError("NO_RICH_A5_FEATURES")
    external_model,external_level_names=train_external_fair(train,session_cache,fair_names)
    trades,trade_diag=load_trades(root,rows,start_ns,end_ns)
    if not trades:raise ValueError("NO_CAUSAL_MAKER_TRADES")

    grid={p:{} for p in POLICIES};receipts={}
    for latency in LATENCIES:
        for horizon in EXITS:
            models,receipt=fit_cell_models(
                train,session_cache,latency,horizon,ext_names,pm_names,full_names,fair_names,external_model,trades)
            receipts[f"{latency}::{horizon}"]=receipt
            cell=evaluate_cell(
                oos,session_cache,trades,models,latency,horizon,
                ext_names,pm_names,full_names,fair_names,external_model)
            for policy,value in cell.items():grid[policy][f"{latency}::{horizon}"]=value

    output.mkdir(parents=True,exist_ok=False)
    manifest={
        "schema":SCHEMA+"_manifest",**SAFETY_PLUS,
        "code_sha":code_sha,"window_start_ns":start_ns,"window_end_ns":end_ns,
        "window_seconds":7200,"train_end_ns":cut,
        "split":{"TRAIN_60":len(train),"LOCKED_OOS_40":len(oos)},
        "selection":window,"feature_join":join_diag,"feature_tape_load":rich_load_diag,
        "anchor_source":feature_diag,
        "external_venue_tape":{"load":external_venue_load_diag,"join":external_venue_join_diag},
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
                        "A4_RESIDUAL":["residual.pm_minus_external","residual.abs","residual.x_tte_s"],
                        "A5_FULL_EXECUTION":full_names},
        "external_fair_feature_names":fair_names,
        "a5_rich_feature_names":rich_a5_names,
        "directional_target_semantics":"PM_YES_AT_DECISION_PLUS_HORIZON_MINUS_PM_YES_AT_DECISION;LATENCY_DOES_NOT_SHIFT_LABEL",
        "grid_cell_count":len(POLICIES)*len(LATENCIES)*len(EXITS),
        "external_fair_level_model_features":external_level_names,
        "model":"RIDGE_FUTURE_PM_REPRICING;SIGN_DRIVES_TOXIC_SIDE_VETO",
        "threshold":"ZERO_PREDICTED_PM_DELTA_FIXED_NO_OOS_TUNING",
    }
    atomic_json(output/"00_manifest.json",manifest)
    atomic_json(output/"01_grid.json",{"schema":SCHEMA+"_grid",**SAFETY_PLUS,"policies":grid})
    atomic_json(output/"02_training_receipts.json",{"schema":SCHEMA+"_training",**SAFETY_PLUS,"cells":receipts})
    mono=monotonicity(grid)
    atomic_json(output/"03_monotonicity.json",{"schema":SCHEMA+"_monotonicity",**SAFETY_PLUS,"policies":mono})

    paired={}
    paired_metrics=(
        "net_pnl_per_posted_share","net_pnl_per_fill_share",
        "fill_probability","toxic_fill_probability","direction_accuracy",
        "executable_markout_per_fill_share",
    )
    for policy in POLICIES[1:]:
        paired[policy]={}
        for key,cell in grid[policy].items():
            base=grid["A0_BASELINE"].get(key) or {}
            deltas={}
            for metric in paired_metrics:
                a=cell.get(metric);b=base.get(metric)
                deltas[metric]=float(a)-float(b) if finite(a) and finite(b) else None
            paired[policy][key]={
                "alpha":{m:cell.get(m) for m in paired_metrics},
                "a0":{m:base.get(m) for m in paired_metrics},
                "delta_alpha_minus_a0":deltas,
            }
    atomic_json(output/"06_paired_vs_a0.json",{
        "schema":SCHEMA+"_paired_vs_a0",**SAFETY_PLUS,
        "comparison":"SAME_LOCKED_OOS_TIMESTAMPS_SAME_LATENCY_SAME_HOLDING_SAME_FROZEN_MAKER_MECHANICS",
        "normalization_note":"PRIMARY_COMPARISON_IS_NET_PNL_PER_POSTED_SHARE_BECAUSE_A0_QUOTES_BOTH_SIDES_AND_ALPHA_POLICIES_QUOTE_ONE_SIDE",
        "policies":paired,
    })

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
    p.add_argument("--external-venue-csv",action="append",default=[])
    p.add_argument("--market-metadata",type=Path,required=True)
    a=p.parse_args(argv)
    try:result=run(
        a.root,a.output_dir,a.code_sha,a.minimum_wall_ns,
        external_venue_csv_specs=a.external_venue_csv,
        market_metadata_path=a.market_metadata)
    except (OSError,ValueError,RuntimeError,np.linalg.LinAlgError) as exc:
        p.exit(2,type(exc).__name__+":"+str(exc)+"\n")
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
