#!/usr/bin/env python3
"""Causal repricing labels using feature origins and exact PM compact event tapes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from v7_multi_crypto_compact_pm_tape import discover_sessions, pair_asof, pair_asof_indexed, session_for_origin
from v7_multi_crypto_repricing_labeler import read_tapes, validate_policy, load_json, quantile

OUTPUT_SCHEMA="polymarket_v7_multi_crypto_compact_repricing_labeled_row_v1"
REPORT_SCHEMA="polymarket_v7_multi_crypto_compact_repricing_label_report_v1"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def parse_utc_ns(raw: Any) -> int:
    try:return int(datetime.fromisoformat(str(raw).replace("Z","+00:00")).timestamp()*1e9)
    except (ValueError,TypeError):return 0


def finite(value: Any) -> float|None:
    try:x=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return x if math.isfinite(x) else None


def atomic_json(path: Path,value: dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n"); os.replace(tmp,path)


def label_rows(origins:list[dict[str,Any]],sessions:list[dict[str,Any]],policy:dict[str,Any])->tuple[list[dict[str,Any]],dict[str,Any]]:
    output=[]; counts={str(h):0 for h in policy["horizons_ms"]}; gaps={str(h):[] for h in policy["horizons_ms"]}
    statuses={str(h):{} for h in policy["horizons_ms"]}; origin_status={}
    for origin in origins:
        market_id=str(origin.get("market_id") or ""); decision_ns=int(origin.get("decision_wall_ns") or 0); decision_ms=decision_ns/1e6
        p0=finite((origin.get("features") or {}).get("pm_yes_mid")); book0=(origin.get("features") or {}).get("pm_book_valid") is True
        session=session_for_origin(sessions,market_id,decision_ms)
        compact0=(pair_asof_indexed(session["indexed_timelines"],market_id,decision_ms) if session and "indexed_timelines" in session
                  else pair_asof(session["timelines"],market_id,decision_ms) if session else None)
        origin_reason="OK"
        if p0 is None or not book0: origin_reason="ORIGIN_BOOK_INVALID"
        elif session is None or compact0 is None: origin_reason="NO_CONTIGUOUS_COMPACT_SESSION"
        else:
            tolerance=2*max(float(compact0["yes_tick_size"]),float(compact0["no_tick_size"]))+1e-12
            if abs(float(compact0["pm_yes"])-p0)>tolerance: origin_reason="ORIGIN_TAPE_MISMATCH"
        origin_status[origin_reason]=origin_status.get(origin_reason,0)+1
        labels={}; end_ns=parse_utc_ns(origin.get("end_timestamp"))
        for h in policy["horizons_ms"]:
            key=str(h); target_ns=decision_ns+int(h)*1_000_000; target_ms=target_ns/1e6; status="NO_CONTIGUOUS_COMPACT_SESSION"; delta=None
            asof_gap=None; target=None
            if origin_reason!="OK": status=origin_reason
            elif end_ns>0 and target_ns>=end_ns: status="CONTRACT_EXPIRES_BEFORE_HORIZON"
            elif session is None or session.get("last_receive_wall_ms") is None or target_ms>float(session["last_receive_wall_ms"]): status="SESSION_END_BEFORE_TARGET"
            else:
                target=(pair_asof_indexed(session["indexed_timelines"],market_id,target_ms) if "indexed_timelines" in session
                        else pair_asof(session["timelines"],market_id,target_ms))
                if target is None: status="TARGET_BOOK_INVALID_OR_MISSING"
                else:
                    asof_gap=target_ms-float(target["state_available_wall_ms"])
                    if asof_gap<0: status="FUTURE_AFTER_TARGET_FORBIDDEN"
                    elif asof_gap>int(policy["maximum_asof_gap_ms"][key]): status="ASOF_GAP_TOO_LARGE"
                    else:
                        delta=float(target["pm_yes"])-p0; status="LABELED"; counts[key]+=1; gaps[key].append(asof_gap)
            statuses[key][status]=statuses[key].get(status,0)+1
            labels[key]={"status":status,"delta_pm_yes":delta,"target_wall_ns":target_ns,"maximum_asof_gap_ms":int(policy["maximum_asof_gap_ms"][key]),"asof_gap_ms":asof_gap,
                         "label_available_wall_ns":None if target is None else int(target["state_available_wall_ms"])*1_000_000,
                         "compact_session_id":None if session is None else session["session_id"]}
        row={"schema":OUTPUT_SCHEMA,"model_sha":origin["model_sha"],"policy_hash":origin["policy_hash"],"feature_schema_hash":origin["feature_schema_hash"],
             "source_identity_hash":origin["source_identity_hash"],"origin_record_hash":origin["record_hash"],"asset":origin.get("asset"),"horizon":origin.get("horizon"),
             "market_id":market_id,"event_id":origin.get("event_id"),"decision_wall_ns":decision_ns,"available_at_ns":origin["available_at_ns"],
             "features":origin["features"],"source_versions":origin.get("source_versions"),"origin_compact_status":origin_reason,
             "origin_compact_session_id":None if session is None else session["session_id"],"labels":labels,
             "paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False}
        row["row_hash"]=canonical_hash(row); output.append(row)
    report={"schema":REPORT_SCHEMA,"paper_only":True,"execution_authority":False,"input_rows":len(origins),"output_rows":len(output),
            "sessions":len(sessions),"nonempty_sessions":sum(int(s["records"]>0) for s in sessions),"origin_status_counts":origin_status,
            "horizons_ms":policy["horizons_ms"],"labeled_counts":counts,
            "coverage":{h:(counts[h]/len(origins) if origins else 0.0) for h in counts},"status_counts":statuses,
            "asof_gap_ms":{h:{"p50":quantile(v,.5),"p95":quantile(v,.95),"max":max(v) if v else None} for h,v in gaps.items()}}
    return output,report


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--feature-tape",type=Path,action="append",required=True); ap.add_argument("--compact-dir",type=Path,required=True)
    ap.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_repricing_label_policy.json")); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--report",type=Path,required=True)
    ap.add_argument("--labeler-code-sha",required=True)
    args=ap.parse_args(); policy=validate_policy(load_json(args.policy)); origins=read_tapes(args.feature_tape)
    if len(args.labeler_code_sha)!=40 or any(ch not in "0123456789abcdef" for ch in args.labeler_code_sha): raise ValueError("exact labeler code SHA required")
    if not origins: raise ValueError("compact_label:no_feature_origins")
    sha=str(origins[0].get("model_sha") or ""); sessions=discover_sessions(args.compact_dir,expected_sha=sha)
    labeled,report=label_rows(origins,sessions,policy); report["input_model_sha"]=sha; report["labeler_code_sha"]=args.labeler_code_sha
    report["compact_session_ids"]=[s["session_id"] for s in sessions]; args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("w",encoding="utf-8") as handle:
        for row in labeled: handle.write(json.dumps(row,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n")
    atomic_json(args.report,report); print(json.dumps(report,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
