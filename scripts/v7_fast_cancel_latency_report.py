#!/usr/bin/env python3
"""Audit realized PAPER external-cancel latency from canonical Maker evidence."""
from __future__ import annotations

import argparse, json, math, pathlib
from collections import defaultdict
from typing import Any

from v7_maker_execution_horse_race import records, num

SCHEMA = "polymarket_v7_fast_cancel_latency_report_v1"


def quantile(values: list[float], p: float) -> float | None:
    if not values: return None
    xs=sorted(values); pos=p*(len(xs)-1); lo,hi=math.floor(pos),math.ceil(pos)
    return xs[lo] if lo==hi else xs[lo]*(hi-pos)+xs[hi]*(pos-lo)


def summarize_ms(values_ns: list[int]) -> dict[str, Any]:
    values=[v/1e6 for v in values_ns if v>=0]
    return {
        "count":len(values), "p50_ms":quantile(values,.50), "p90_ms":quantile(values,.90),
        "p99_ms":quantile(values,.99), "max_ms":max(values) if values else None,
        "fraction_le_25ms":sum(v<=25 for v in values)/len(values) if values else None,
        "fraction_le_50ms":sum(v<=50 for v in values)/len(values) if values else None,
        "fraction_le_100ms":sum(v<=100 for v in values)/len(values) if values else None,
    }


def cancel_samples(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str,int]]:
    out=[]; d=defaultdict(int)
    for r in rows:
        if r.get("event_type")!="ORDER_STATE" or str(r.get("order_state") or "")!="CANCEL_REQUESTED": continue
        d["cancel_requested_rows"]+=1
        m=r.get("metadata") if isinstance(r.get("metadata"),dict) else {}
        env=m.get("external_cancel_opportunity_envelope") if isinstance(m.get("external_cancel_opportunity_envelope"),dict) else None
        if not env or env.get("action")!="CANCEL" or "RESEARCH_CANCEL_RULE_MATCH" not in (env.get("reasons") or []): d["non_external_cancel_rows"]+=1; continue
        stamps=env.get("source_event_timestamps_ns") if isinstance(env.get("source_event_timestamps_ns"),list) else []
        trigger=int(num(stamps[0],0)) if stamps else 0; decision=int(num(env.get("decision_receive_timestamp_ns"),0)); recorded_ms=int(num(r.get("recorded_ts_ms"),0))
        if trigger<=0 or decision<=0 or recorded_ms<=0: d["missing_timestamp_rows"]+=1; continue
        # recorded_ts_ms is millisecond-granularity. Add 999,999 ns so the
        # reported end-to-end latency is a conservative upper bound.
        request_upper=recorded_ms*1_000_000+999_999
        if decision<trigger or request_upper<trigger: d["invalid_timestamp_order"]+=1; continue
        out.append({
            "record_id":str(r.get("record_id") or ""), "market_id":str(r.get("market_id") or ""),
            "event_id":str(r.get("event_id") or r.get("market_id") or "UNKNOWN"),
            "replay_key":str(env.get("deterministic_replay_key") or ""),
            "trigger_wall_ns":trigger, "decision_receive_ns":decision,
            "cancel_request_upper_wall_ns":request_upper,
            "trigger_to_cancel_request_upper_ns":request_upper-trigger,
            "decision_to_cancel_request_upper_ns":max(0,request_upper-decision),
        })
    d["usable_external_cancel_rows"]=len(out)
    out.sort(key=lambda x:(x["trigger_wall_ns"],x["record_id"]))
    return out,dict(d)


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples,diag=cancel_samples(rows)
    trigger=[x["trigger_to_cancel_request_upper_ns"] for x in samples]; decision=[x["decision_to_cancel_request_upper_ns"] for x in samples]
    by_market:defaultdict[str,list[int]]=defaultdict(list)
    for x in samples: by_market[x["market_id"]].append(x["trigger_to_cancel_request_upper_ns"])
    return {
        "schema":SCHEMA,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","timestamp_precision":"CANCEL_REQUEST_WALL_TIME_UPPER_BOUNDED_TO_PLUS_0_999999MS",
        "diagnostics":diag,"markets":len(by_market),
        "trigger_to_cancel_request":summarize_ms(trigger),"decision_to_cancel_request":summarize_ms(decision),
        "market_p99_ms":{k:summarize_ms(v)["p99_ms"] for k,v in sorted(by_market.items())},
        "latency_gate_interpretation":{
            "green":"realized p99 trigger-to-cancel-request materially below 100ms frozen signal-age budget",
            "red":"p99 near/above 100ms: remove Python/filesystem polling from fast lane before broader PAPER use",
        },
    }


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--maker-evidence",type=pathlib.Path,action="append",required=True); ap.add_argument("--output",type=pathlib.Path,required=True); a=ap.parse_args()
    report=build_report(records(a.maker_evidence)); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8"); print(json.dumps(report,indent=2,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
