#!/usr/bin/env python3
"""Zero-authority latency/capacity replay on exact compact PM L1 states."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from v7_multi_crypto_compact_pm_tape import discover_sessions, pair_asof_indexed, session_for_origin
from v7_multi_crypto_repricing_labeler import read_tapes

POLICY_SCHEMA="polymarket_v7_multi_crypto_latency_capacity_policy_v1"
REPORT_SCHEMA="polymarket_v7_multi_crypto_latency_capacity_report_v1"
CANDIDATE_SCHEMA="polymarket_v7_multi_crypto_research_candidate_v1"


def load_json(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict):raise ValueError(f"{path}: object required")
    return value


def atomic_json(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8");os.replace(tmp,path)


def canonical_hash(value:Any)->str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def validate_policy(v:dict[str,Any])->dict[str,Any]:
    if (v.get("schema")!=POLICY_SCHEMA or v.get("version")!=1 or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False or v.get("real_order_submission") is not False
        or v.get("execution_authority") is not False or v.get("research_only") is not True
        or v.get("economic_evidence") is not False or v.get("mode")!="EXECUTION_MECHANICS_ONLY"
        or v.get("limit_policy")!="ORIGINAL_BEST_ASK_NO_CHASE" or v.get("fill_policy")!="FAK_L1_ONLY_CANCEL_REMAINDER"
        or v.get("depth_policy")!="L1_AT_MODELED_ARRIVAL" or v.get("profit_claim")!="FORBIDDEN_WITHOUT_CALIBRATED_SIGNAL_AND_COST_MODEL"):
        raise ValueError("latency_capacity_policy_identity_or_authority")
    delays=v.get("additional_latency_ms");sizes=v.get("size_shares")
    if not isinstance(delays,list) or delays!=sorted(set(int(x) for x in delays)) or any(int(x)<0 for x in delays):raise ValueError("latency_capacity_delays")
    if not isinstance(sizes,list) or sizes!=sorted(set(float(x) for x in sizes)) or any(float(x)<=0 for x in sizes):raise ValueError("latency_capacity_sizes")
    if v.get("venue_matching_delay_ms") is not None or v.get("matching_delay_policy")!="UNCONFIGURED_NOT_APPLIED":raise ValueError("latency_capacity_matching_delay")
    if int(v.get("mechanics_origin_min_interval_ms") or 0)<1:raise ValueError("latency_capacity_origin_interval")
    return v


def mechanics_candidates(feature_rows:list[dict[str,Any]],sessions:list[dict[str,Any]],policy:dict[str,Any])->list[dict[str,Any]]:
    minimum_ns=int(policy["mechanics_origin_min_interval_ms"])*1_000_000;last:dict[str,int]={};output=[]
    for row in sorted(feature_rows,key=lambda r:int(r.get("decision_wall_ns") or 0)):
        market=str(row.get("market_id") or "");decision=int(row.get("decision_wall_ns") or 0);features=row.get("features") if isinstance(row.get("features"),dict) else {}
        if not market or decision<=0 or features.get("pm_book_valid") is not True:continue
        if last.get(market,0) and decision-last[market]<minimum_ns:continue
        session=session_for_origin(sessions,market,decision/1e6)
        if session is None:continue
        state=pair_asof_indexed(session["indexed_timelines"],market,decision/1e6)
        if state is None:continue
        origin_mid=features.get("pm_yes_mid")
        try:origin_mid=float(origin_mid)
        except (TypeError,ValueError,OverflowError):continue
        tol=2*max(float(state["yes_tick_size"]),float(state["no_tick_size"]))+1e-12
        if not math.isfinite(origin_mid) or abs(origin_mid-float(state["pm_yes"]))>tol:continue
        source_hash=str(row.get("record_hash") or row.get("source_identity_hash") or "")
        for outcome in ("YES","NO"):
            limit=float(state["yes_best_ask"] if outcome=="YES" else state["no_best_ask"])
            candidate={"schema":CANDIDATE_SCHEMA,"candidate_mode":"MECHANICS_PROBE","candidate_id":canonical_hash([source_hash,outcome]),
                "competition_group_id":canonical_hash([source_hash,outcome,"independent"]),"model_sha":row.get("model_sha"),"asset":row.get("asset"),"horizon":row.get("horizon"),
                "market_id":market,"event_id":row.get("event_id"),"outcome":outcome,"decision_wall_ns":decision,"original_limit_price":limit,
                "source_record_hash":source_hash,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,"economic_signal":False}
            output.append(candidate)
        last[market]=decision
    return output


def validate_candidate(v:dict[str,Any],expected_sha:str)->dict[str,Any]:
    if (v.get("schema")!=CANDIDATE_SCHEMA or v.get("paper_only") is not True or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False or v.get("execution_authority") is not False or v.get("model_sha")!=expected_sha
        or v.get("outcome") not in {"YES","NO"} or not str(v.get("candidate_id") or "") or not str(v.get("competition_group_id") or "")
        or int(v.get("decision_wall_ns") or 0)<=0):raise ValueError("latency_capacity_candidate_identity")
    limit=float(v.get("original_limit_price") or 0)
    if not 0<limit<1:raise ValueError("latency_capacity_candidate_limit")
    return v


def arrival(candidate:dict[str,Any],delay_ms:int,sessions:list[dict[str,Any]])->dict[str,Any]:
    decision_ms=int(candidate["decision_wall_ns"])/1e6;session=session_for_origin(sessions,str(candidate["market_id"]),decision_ms)
    if session is None:return {"status":"NO_ORIGIN_SESSION"}
    target_ms=decision_ms+delay_ms
    last=session.get("last_receive_wall_ms")
    if last is None or target_ms>float(last):return {"status":"SESSION_END_BEFORE_ARRIVAL","session_id":session["session_id"]}
    state=pair_asof_indexed(session["indexed_timelines"],str(candidate["market_id"]),target_ms)
    if state is None:return {"status":"ARRIVAL_BOOK_INVALID","session_id":session["session_id"]}
    outcome=str(candidate["outcome"]);prefix="yes" if outcome=="YES" else "no"
    ask=float(state[f"{prefix}_best_ask"]);depth=state.get(f"{prefix}_ask_depth_l1");sequence=int(state[f"{prefix}_sequence"])
    if depth is None or not math.isfinite(float(depth)) or float(depth)<0:return {"status":"ARRIVAL_DEPTH_MISSING","session_id":session["session_id"]}
    return {"status":"OBSERVED","session_id":session["session_id"],"target_wall_ms":target_ms,"arrival_ask":ask,"arrival_depth_l1":float(depth),"state_sequence":sequence,
            "state_available_wall_ms":float(state["state_available_wall_ms"]),"asof_gap_ms":target_ms-float(state["state_available_wall_ms"])}


def scenario(candidates:list[dict[str,Any]],sessions:list[dict[str,Any]],delay_ms:int,size:float)->dict[str,Any]:
    counters={"candidate_count":len(candidates),"observed_arrival_count":0,"full_fill_count":0,"partial_fill_count":0,"no_fill_worse_price_count":0,"no_fill_zero_depth_count":0,"unobserved_count":0}
    requested=0.0;filled=0.0;notional=0.0;improvement=0.0;consumed:dict[tuple[Any,...],float]={}
    for candidate in sorted(candidates,key=lambda c:(int(c["decision_wall_ns"]),str(c["candidate_id"]))):
        requested+=size;arr=arrival(candidate,delay_ms,sessions)
        if arr["status"]!="OBSERVED":counters["unobserved_count"]+=1;continue
        counters["observed_arrival_count"]+=1;limit=float(candidate["original_limit_price"]);ask=float(arr["arrival_ask"])
        if ask>limit+1e-12:counters["no_fill_worse_price_count"]+=1;continue
        key=(candidate["competition_group_id"],arr["session_id"],candidate["market_id"],candidate["outcome"],arr["state_sequence"])
        remaining=max(0.0,float(arr["arrival_depth_l1"])-consumed.get(key,0.0));fill=min(size,remaining)
        if fill<=0:counters["no_fill_zero_depth_count"]+=1;continue
        consumed[key]=consumed.get(key,0.0)+fill;filled+=fill;notional+=fill*ask;improvement+=fill*(limit-ask)
        if fill+1e-12>=size:counters["full_fill_count"]+=1
        else:counters["partial_fill_count"]+=1
    return {**counters,"delay_ms":delay_ms,"size_shares":size,"requested_shares":requested,"filled_shares":filled,"fill_fraction_requested":filled/requested if requested else 0.0,
            "average_fill_price":notional/filled if filled else None,"average_price_improvement_vs_limit":improvement/filled if filled else None}


def common_observable_ids(candidates:list[dict[str,Any]],sessions:list[dict[str,Any]],delays:list[int])->set[str]:
    output=set()
    for candidate in candidates:
        if all(arrival(candidate,d,sessions).get("status")=="OBSERVED" for d in delays):output.add(str(candidate["candidate_id"]))
    return output


def read_candidate_tape(path:Path,expected_sha:str)->list[dict[str,Any]]:
    rows=[];seen=set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():continue
            value=validate_candidate(json.loads(line),expected_sha);cid=str(value["candidate_id"])
            if cid in seen:raise ValueError("latency_capacity_duplicate_candidate")
            seen.add(cid);rows.append(value)
    return rows


def file_sha(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""):digest.update(chunk)
    return digest.hexdigest()


def replay(candidates:list[dict[str,Any]],sessions:list[dict[str,Any]],policy:dict[str,Any],*,candidate_mode:str,lineage:dict[str,Any])->dict[str,Any]:
    delays=[int(x) for x in policy["additional_latency_ms"]];sizes=[float(x) for x in policy["size_shares"]];common=common_observable_ids(candidates,sessions,delays)
    curves={};common_curves={};common_candidates=[c for c in candidates if str(c["candidate_id"]) in common]
    for delay in delays:
        curves[str(delay)]={str(size):scenario(candidates,sessions,delay,size) for size in sizes}
        common_curves[str(delay)]={str(size):scenario(common_candidates,sessions,delay,size) for size in sizes}
    return {"schema":REPORT_SCHEMA,"version":1,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,
            "research_only":True,"economic_evidence":False,"status":"RESEARCH_EXECUTION_MECHANICS_ONLY","candidate_mode":candidate_mode,
            "matching_delay_policy":policy["matching_delay_policy"],"venue_matching_delay_ms":None,"limit_policy":policy["limit_policy"],"fill_policy":policy["fill_policy"],"depth_policy":policy["depth_policy"],
            "candidate_count":len(candidates),"common_observable_candidate_count":len(common),"additional_latency_ms":delays,"size_shares":sizes,
            "curves":curves,"common_sample_curves":common_curves,"lineage":lineage,
            "limitations":["L1-only modeled arrival liquidity","public-book PAPER/SHADOW evidence; not real matching-engine fills","MECHANICS_PROBE has no economic side selection or PnL claim" if candidate_mode=="MECHANICS_PROBE" else "candidate tape replay still does not prove real fill behavior","venue matching delay not applied because it is not bound in this policy"]}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--compact-dir",type=Path,required=True);parser.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_latency_capacity.json"));parser.add_argument("--output",type=Path,required=True);parser.add_argument("--code-sha",required=True)
    parser.add_argument("--feature-tape",type=Path,action="append",default=[]);parser.add_argument("--candidate-tape",type=Path)
    args=parser.parse_args();policy=validate_policy(load_json(args.policy))
    if len(args.code_sha)!=40 or any(ch not in "0123456789abcdef" for ch in args.code_sha):raise ValueError("exact code SHA required")
    if bool(args.feature_tape)==bool(args.candidate_tape):raise ValueError("choose exactly one of feature-tape mechanics probe or candidate-tape")
    if args.candidate_tape:
        first_manifest=next(iter(sorted(args.compact_dir.glob("*.manifest.json"))),None)
        if first_manifest is None:raise ValueError("compact label sessions missing")
        manifest=load_json(first_manifest);producer_sha=str(manifest.get("model_sha") or "");candidates=read_candidate_tape(args.candidate_tape,producer_sha);mode="CANDIDATE_TAPE";lineage={"candidate_tape":str(args.candidate_tape),"candidate_tape_sha256":file_sha(args.candidate_tape)}
    else:
        features=read_tapes(args.feature_tape)
        if not features:raise ValueError("mechanics feature tape empty")
        producer_sha=str(features[0].get("model_sha") or "");mode="MECHANICS_PROBE";lineage={"feature_tapes":{str(p):file_sha(p) for p in args.feature_tape}}
    sessions=discover_sessions(args.compact_dir,expected_sha=producer_sha)
    if not args.candidate_tape:candidates=mechanics_candidates(features,sessions,policy)
    report=replay(candidates,sessions,policy,candidate_mode=mode,lineage={**lineage,"producer_model_sha":producer_sha,"replay_code_sha":args.code_sha,"compact_session_ids":[s["session_id"] for s in sessions]})
    atomic_json(args.output,report);print(json.dumps(report,sort_keys=True));return 0

if __name__=="__main__":raise SystemExit(main())
