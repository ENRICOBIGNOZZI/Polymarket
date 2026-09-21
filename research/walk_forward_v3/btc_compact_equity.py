"""BTC executable timing equities from the continuous compact PM book tape.

PAPER-only research. Uses last valid receive-time L1 state at or before each
entry/exit target, inside one continuous observer session. No interpolation,
no future-after-target state and no trading authority.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import json
import math
from pathlib import Path

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, fee_per_share, json_lines
from research.walk_forward_v3.direct_action import (
    DEFAULT_HARD_ORDER_NOTIONAL,
    _valid_state,
    decision_side_state,
    selected_action_side,
)
from scripts.v7_multi_crypto_compact_pm_tape import (
    load_manifest,
    record_struct,
    token_map,
    validate_status,
    build_indexed_timelines,
    pair_asof_indexed,
)

SCHEMA="polymarket_v7_btc_compact_timing_equity_v1"
LATENCIES=(10,25,50,100,250)
EXITS=(500,750,1000,1500,2000,3000,4000,5000)
SIZE=5.0


def market_halves(rows):
    first={}
    for row in rows:
        market=str(row["market_id"])
        first[market]=min(first.get(market,int(row["decision_ns"])),int(row["decision_ns"]))
    ordered=sorted(first,key=lambda m:(first[m],m))
    cut=max(1,len(ordered)//2)
    return set(ordered[:cut]),set(ordered[cut:])


def compact_dirs(root):
    out=set()
    for path in root.rglob("*.manifest.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            load_manifest(path)
        except (OSError,ValueError,TypeError,json.JSONDecodeError):
            continue
        out.add(path.parent)
    return sorted(out)


def stream_sessions(root, rows):
    market_windows={}
    for row in rows:
        market=str(row["market_id"])
        decision_ms=int(row["decision_ns"])//1_000_000
        lo,hi=market_windows.get(market,(decision_ms,decision_ms))
        market_windows[market]=(min(lo,decision_ms),max(hi,decision_ms))
    sessions=[]
    diagnostics={"directories_seen":0,"manifests_seen":0,"sessions_loaded":0,
                 "records_scanned":0,"records_retained":0,"sessions_rejected":0}
    for directory in compact_dirs(root):
        diagnostics["directories_seen"]+=1
        for manifest_path in sorted(directory.glob("*.manifest.json")):
            diagnostics["manifests_seen"]+=1
            try:
                manifest=load_manifest(manifest_path)
                session_id=str(manifest.get("observer_session_id") or "")
                status_path=directory/f"{session_id}.status.json"
                if not session_id or not status_path.is_file():
                    raise ValueError("status missing")
                status=json.loads(status_path.read_text(encoding="utf-8"))
                validate_status(status,manifest,require_no_reconnect=True)
                mapping=token_map(manifest)
                handles={
                    handle:meta for handle,meta in mapping.items()
                    if str(meta.get("market_id") or "") in market_windows
                }
                if not handles:
                    continue
                watermark=int(status.get("book_watermark_receive_wall_ms") or 0)
                if watermark<=0:
                    raise ValueError("watermark missing")
                record=record_struct(manifest)
                tape_paths=sorted(directory.glob(f"{session_id}.segment-*.bin"))
                current=directory/f"{session_id}.current.bin"
                if current.is_file():
                    tape_paths.append(current)
                if not tape_paths:
                    raise ValueError("tape missing")
                relevant=[]
                prior={}
                previous_sequence=0
                scanned=0
                first_wall=None
                last_wall=None
                for path in tape_paths:
                    payload=path.read_bytes()
                    usable=(len(payload)//record.size)*record.size
                    if path!=current and usable!=len(payload):
                        raise ValueError("sealed partial record")
                    for offset in range(0,usable,record.size):
                        values=record.unpack_from(payload,offset)
                        scanned+=1
                        if len(values)==15:
                            seq,handle,state_version,epoch,wall_ms,mono_ns,bid_e4,ask_e4,tick_e4,bid_depth,ask_depth,valid,lineage,kind,_=values
                        else:
                            seq,handle,state_version,epoch,wall_ms,mono_ns,bid_e4,ask_e4,tick_e4,valid,lineage,kind,_=values
                            bid_depth=ask_depth=None
                        if seq<=previous_sequence:
                            raise ValueError("nonmonotone compact sequence")
                        previous_sequence=seq
                        first_wall=wall_ms if first_wall is None else min(first_wall,wall_ms)
                        last_wall=wall_ms if last_wall is None else max(last_wall,wall_ms)
                        meta=handles.get(handle)
                        if meta is None:
                            continue
                        market=str(meta["market_id"])
                        lo,hi=market_windows[market]
                        start=lo-1000
                        end=hi+max(EXITS)+1000
                        row={
                            "observer_sequence":int(seq),"instrument_handle":int(handle),
                            "state_version":int(state_version),"connection_epoch":int(epoch),
                            "receive_wall_ms":int(wall_ms),"receive_monotonic_ns":int(mono_ns),
                            "best_bid":float(bid_e4)/10000.0,"best_ask":float(ask_e4)/10000.0,
                            "tick_size":float(tick_e4)/10000.0,
                            "bid_depth_l1":None if bid_depth is None else float(bid_depth)/1_000_000.0,
                            "ask_depth_l1":None if ask_depth is None else float(ask_depth)/1_000_000.0,
                            "valid":bool(valid),"lineage_continuous":bool(lineage),"event_kind":int(kind),
                            "market_id":market,"event_id":str(meta.get("event_id") or ""),
                            "token_id":str(meta["token_id"]),"outcome":str(meta["outcome"]),
                        }
                        if wall_ms<start:
                            prior[handle]=row
                        elif wall_ms<=end:
                            relevant.append(row)
                relevant.extend(prior.values())
                if not relevant:
                    continue
                indexed=build_indexed_timelines(relevant)
                sessions.append({
                    "session_id":session_id,"directory":str(directory),
                    "watermark_ms":watermark,"first_wall_ms":first_wall,
                    "last_wall_ms":last_wall,"indexed":indexed,
                    "records_scanned":scanned,"records_retained":len(relevant),
                })
                diagnostics["sessions_loaded"]+=1
                diagnostics["records_scanned"]+=scanned
                diagnostics["records_retained"]+=len(relevant)
            except (OSError,ValueError,TypeError,json.JSONDecodeError):
                diagnostics["sessions_rejected"]+=1
    return sessions,diagnostics



def stream_raw_sessions(root, rows):
    book_root=Path(root)/"research/repricing_book/book_observations"
    diagnostics={"source":"RAW_CAUSAL_BOOK_JSONL","book_root":str(book_root),
                 "files_seen":0,"rows_seen":0,"rows_retained":0,
                 "sessions_loaded":0,"sequence_gaps":0,"lineage_breaks":0}
    if not book_root.is_dir() or book_root.is_symlink():
        return [],diagnostics
    market_tokens=defaultdict(set)
    windows={}
    for row in rows:
        market=str(row["market_id"])
        for token in (row.get("yes_token_id"),row.get("no_token_id"),row.get("token_id")):
            if token:
                market_tokens[market].add(str(token))
        decision_ms=int(row["decision_ns"])//1_000_000
        lo,hi=windows.get(market,(decision_ms,decision_ms))
        windows[market]=(min(lo,decision_ms),max(hi,decision_ms))
    paths=[p for p in book_root.glob("*.jsonl*") if p.is_file() and not p.is_symlink()]
    paths.sort(key=lambda p:(p.name=="current.jsonl",p.name))
    sessions={}
    for path in paths:
        diagnostics["files_seen"]+=1
        for raw in json_lines(path):
            if not isinstance(raw,dict) or raw.get("schema")!="polymarket_v7_causal_book_observation_v1":
                continue
            diagnostics["rows_seen"]+=1
            try:
                sid=str(raw["observer_session_id"]); epoch=int(raw["connection_epoch"])
                seq=int(raw["observer_sequence"]); wall=int(raw["receive_wall_ms"])
                market=str(raw["market_id"]); token=str(raw["token_id"])
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
            key=(sid,epoch)
            state=sessions.setdefault(key,{"session_id":sid,"epoch":epoch,"last_seq":None,
                                           "span":0,"span_watermark":defaultdict(int),
                                           "rows":[],"first_wall_ms":None,"last_wall_ms":None})
            prior=state["last_seq"]
            if prior is not None and seq!=prior+1:
                state["span"]+=1; diagnostics["sequence_gaps"]+=1
            state["last_seq"]=seq
            state["first_wall_ms"]=wall if state["first_wall_ms"] is None else min(state["first_wall_ms"],wall)
            state["last_wall_ms"]=wall if state["last_wall_ms"] is None else max(state["last_wall_ms"],wall)
            lineage=raw.get("lineage_continuous") is True
            if not lineage:
                state["span"]+=1; diagnostics["lineage_breaks"]+=1
                continue
            span=state["span"]
            state["span_watermark"][span]=max(state["span_watermark"][span],wall)
            if market not in windows or token not in market_tokens[market]:
                continue
            lo,hi=windows[market]
            if not (lo-5000<=wall<=hi+max(EXITS)+5000):
                continue
            try:
                bid=float(raw["best_bid"]); ask=float(raw["best_ask"])
                bid_depth=float(raw.get("bid_depth_l1") or 0.0)
                ask_depth=float(raw.get("ask_depth_l1") or 0.0)
                tick=float(raw.get("tick_size") or .01)
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
            if raw.get("valid") is not True or not (0<bid<ask<1 and bid_depth>=0 and ask_depth>=0 and tick>0):
                continue
            state["rows"].append({"market_id":market,"token_id":token,"time_ms":wall,
                                  "bid":bid,"ask":ask,"bid_depth":bid_depth,
                                  "ask_depth":ask_depth,"tick":tick,"span":span})
            diagnostics["rows_retained"]+=1
    output=[]
    for state in sessions.values():
        if not state["rows"]:
            continue
        indexed=defaultdict(list)
        for row in state["rows"]:
            indexed[(row["market_id"],row["token_id"])].append(row)
        index={}
        for key,seq in indexed.items():
            seq.sort(key=lambda r:r["time_ms"])
            index[key]={"rows":seq,"stamps":[r["time_ms"] for r in seq]}
        state["raw_index"]=dict(index)
        output.append(state)
    diagnostics["sessions_loaded"]=len(output)
    return output,diagnostics


def raw_token_asof(session, market, token, target_ms):
    idx=session.get("raw_index",{}).get((market,token))
    if not idx:
        return None
    pos=bisect_right(idx["stamps"],target_ms)-1
    if pos<0:
        return None
    row=idx["rows"][pos]
    if session["span_watermark"].get(row["span"],0)<target_ms:
        return None
    return row


def pair_asof_session(session, row, target_ms):
    market=str(row["market_id"])
    if "indexed" in session:
        return pair_asof_indexed(session["indexed"],market,target_ms)
    yes_token=str(row.get("yes_token_id") or "")
    no_token=str(row.get("no_token_id") or "")
    yes=raw_token_asof(session,market,yes_token,target_ms)
    no=raw_token_asof(session,market,no_token,target_ms)
    if yes is None or no is None or yes["span"]!=no["span"]:
        return None
    yes_mid=(yes["bid"]+yes["ask"])/2.0
    no_mid=(no["bid"]+no["ask"])/2.0
    tolerance=2.0*max(yes["tick"],no["tick"])+1e-12
    if abs(yes_mid+no_mid-1.0)>tolerance:
        return None
    return {"pm_yes":(yes_mid+1.0-no_mid)/2.0,
            "yes_best_bid":yes["bid"],"yes_best_ask":yes["ask"],
            "no_best_bid":no["bid"],"no_best_ask":no["ask"],
            "yes_bid_depth_l1":yes["bid_depth"],"yes_ask_depth_l1":yes["ask_depth"],
            "no_bid_depth_l1":no["bid_depth"],"no_ask_depth_l1":no["ask_depth"],
            "connection_epoch":session["epoch"],
            "state_available_wall_ms":max(yes["time_ms"],no["time_ms"])}

def session_for_row(sessions,row):
    origin_ms=int(row["decision_ns"])/1_000_000.0
    candidates=[]
    for session in sessions:
        watermark=session.get("watermark_ms",session.get("last_wall_ms") or 0)
        if watermark<origin_ms:
            continue
        pair=pair_asof_session(session,row,origin_ms)
        if pair is not None:
            candidates.append(session)
    if len(candidates)!=1:
        return None,"NO_CONTINUOUS_BOOK_SESSION" if not candidates else "OVERLAPPING_BOOK_SESSIONS"
    return candidates[0],None


def side_state(pair,side):
    prefix="yes" if str(side)=="YES" else "no"
    bid=pair.get(f"{prefix}_best_bid")
    ask=pair.get(f"{prefix}_best_ask")
    bid_depth=pair.get(f"{prefix}_bid_depth_l1")
    ask_depth=pair.get(f"{prefix}_ask_depth_l1")
    vals=(bid,ask,bid_depth,ask_depth)
    if any(v is None or not math.isfinite(float(v)) for v in vals):
        return None
    if not (0<float(bid)<float(ask)<1 and float(bid_depth)>=0 and float(ask_depth)>=0):
        return None
    return {"bid":float(bid),"ask":float(ask),
            "bid_depth":float(bid_depth),"ask_depth":float(ask_depth)}


def execute_cell(row,session,latency_ms,exit_ms):
    side=selected_action_side(row)
    decision=decision_side_state(row,side)
    if decision is None:
        return None,"SIDE_DECISION_EVIDENCE_UNAVAILABLE"
    ask0=float(decision["ask"])
    depth0=float(decision["ask_quantity"])
    if float(row["minimum"])>SIZE+1e-12:
        return None,"VENUE_MINIMUM_ABOVE_TARGET_SIZE"
    if SIZE>depth0+1e-12 or SIZE*ask0>DEFAULT_HARD_ORDER_NOTIONAL+1e-9 or ask0>.99:
        return None,"SIZE_DEPTH_OR_NOTIONAL_CAP"
    origin_ms=int(row["decision_ns"])/1_000_000.0
    arrival_ms=origin_ms+latency_ms
    exit_target_ms=origin_ms+exit_ms
    watermark=session.get("watermark_ms",session.get("last_wall_ms") or 0)
    if watermark<exit_target_ms:
        return None,"SESSION_WATERMARK_BEFORE_EXIT"
    arrival_pair=pair_asof_session(session,row,arrival_ms)
    if arrival_pair is None:
        return None,"ARRIVAL_ASOF_UNAVAILABLE"
    exit_pair=pair_asof_session(session,row,exit_target_ms)
    if exit_pair is None:
        return None,"EXIT_ASOF_UNAVAILABLE"
    arrival=side_state(arrival_pair,side)
    exit_state=side_state(exit_pair,side)
    if arrival is None:
        return None,"ARRIVAL_L1_DEPTH_UNAVAILABLE"
    if exit_state is None:
        return None,"EXIT_L1_DEPTH_UNAVAILABLE"
    if row.get("epoch") and int(arrival_pair["connection_epoch"])!=int(row["epoch"]):
        return None,"ENTRY_EPOCH_MISMATCH"
    if int(exit_pair["connection_epoch"])!=int(arrival_pair["connection_epoch"]):
        return None,"EXIT_EPOCH_MISMATCH"
    if arrival["ask"]>ask0+1e-12:
        return {"cash_pnl":0.0,"filled":0.0,"exit_filled":0.0,
                "entry_price":None,"exit_bid":None,"side":side,
                "arrival_state_available_ms":arrival_pair["state_available_wall_ms"],
                "exit_state_available_ms":exit_pair["state_available_wall_ms"]},"OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    fill=min(SIZE,arrival["ask_depth"])
    if fill<=0:
        return {"cash_pnl":0.0,"filled":0.0,"exit_filled":0.0,
                "entry_price":None,"exit_bid":None,"side":side,
                "arrival_state_available_ms":arrival_pair["state_available_wall_ms"],
                "exit_state_available_ms":exit_pair["state_available_wall_ms"]},"OBSERVED_NO_FILL_ZERO_DEPTH"
    exit_fill=min(fill,exit_state["bid_depth"])
    entry_price=arrival["ask"]
    exit_bid=exit_state["bid"]
    entry_fee=fill*fee_per_share(row,entry_price)
    exit_fee=exit_fill*fee_per_share(row,exit_bid)
    cash=exit_fill*exit_bid-fill*entry_price-entry_fee-exit_fee
    return {
        "cash_pnl":float(cash),"filled":float(fill),"exit_filled":float(exit_fill),
        "residual_inventory":float(max(0.0,fill-exit_fill)),
        "entry_price":float(entry_price),"exit_bid":float(exit_bid),"side":str(side),
        "arrival_state_available_ms":int(arrival_pair["state_available_wall_ms"]),
        "exit_state_available_ms":int(exit_pair["state_available_wall_ms"]),
        "arrival_asof_gap_ms":float(arrival_ms-arrival_pair["state_available_wall_ms"]),
        "exit_asof_gap_ms":float(exit_target_ms-exit_pair["state_available_wall_ms"]),
    },"OBSERVED_FULL_FILL" if fill+1e-12>=SIZE else "OBSERVED_PARTIAL_FILL"


def analyze(root,minimum_wall_ns):
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,
                       include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        return {"schema":SCHEMA,**SAFETY,"state":data.get("input_state")}
    rows=[r for r in data["decisions"] if _valid_state(r) and str(r.get("asset"))=="BTC"]
    sessions,tape_diag=stream_sessions(Path(root).resolve().parent,rows)
    if not sessions:
        sessions,tape_diag=stream_raw_sessions(Path(root),rows)
    discovery,validation=market_halves(rows)
    output={"schema":SCHEMA,**SAFETY,"state":"READY","asset":"BTC",
            "latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),
            "target_size_shares":SIZE,"data_sha256":data.get("data_sha256"),
            "tape_diagnostics":tape_diag,"splits":{}}
    session_cache={}
    for row in rows:
        session_cache[row["decision_id"]]=session_for_row(sessions,row)
    for name,markets in (("DISCOVERY",discovery),("VALIDATION",validation)):
        events=defaultdict(list); metrics={}; censored=defaultdict(lambda:defaultdict(int))
        split_rows=[r for r in rows if str(r["market_id"]) in markets]
        for latency in LATENCIES:
            for exit_ms in EXITS:
                key=f"{latency}::{exit_ms}"
                pnl=0.0; actions=fills=positive=negative=zero=0
                for row in split_rows:
                    session,why=session_cache[row["decision_id"]]
                    if session is None:
                        censored[key][why]+=1
                        continue
                    economics,state=execute_cell(row,session,latency,exit_ms)
                    if economics is None:
                        censored[key][state]+=1
                        continue
                    actions+=1
                    value=float(economics["cash_pnl"])
                    pnl+=value
                    fill=float(economics.get("filled") or 0.0)
                    if fill>0:
                        fills+=1
                        event={"decision_ns":int(row["decision_ns"]),
                               "decision_id":str(row["decision_id"]),
                               "market_id":str(row["market_id"]),
                               "contract_horizon":str(row.get("horizon") or ""),
                               "cash_pnl":value,"execution_state":state,**economics}
                        events[key].append(event)
                    if value>1e-15: positive+=1
                    elif value<-1e-15: negative+=1
                    else: zero+=1
                events[key].sort(key=lambda e:(e["decision_ns"],e["decision_id"]))
                metrics[key]={"actions":actions,"fills":fills,"pnl":pnl,
                              "fill_rate":fills/actions if actions else None,
                              "pnl_per_fill":pnl/fills if fills else None,
                              "positive":positive,"negative":negative,"zero":zero}
        output["splits"][name]={
            "decision_rows":len(split_rows),"metrics":metrics,
            "equity_events":dict(events),
            "censored":{k:dict(v) for k,v in censored.items()},
        }
    return output


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    out=analyze(a.root,a.minimum_wall_ns)
    atomic_json(a.output,out)
    return 0 if out.get("state")=="READY" else 2


if __name__=="__main__":
    raise SystemExit(main())
