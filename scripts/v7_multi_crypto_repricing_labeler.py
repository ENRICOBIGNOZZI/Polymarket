#!/usr/bin/env python3
"""Offline causal-asof repricing labels for multi-crypto SHADOW feature tapes."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

TAPE_SCHEMA="polymarket_v7_multi_crypto_feature_tape_v1"
POLICY_SCHEMA="polymarket_v7_multi_crypto_repricing_label_policy_v1"
OUTPUT_SCHEMA="polymarket_v7_multi_crypto_repricing_labeled_row_v1"
REPORT_SCHEMA="polymarket_v7_multi_crypto_repricing_label_report_v1"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def load_json(path: Path) -> dict[str,Any]:
    v=json.loads(path.read_text());
    if not isinstance(v,dict): raise ValueError(f"{path}: object required")
    return v


def atomic_json(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n"); os.replace(tmp,path)


def parse_utc_ns(raw: Any) -> int:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return int(stamp.timestamp() * 1e9) if stamp.tzinfo is not None else 0
    except (ValueError, TypeError):
        return 0


def validate_policy(v: dict[str,Any]) -> dict[str,Any]:
    if (v.get("schema")!=POLICY_SCHEMA or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False or v.get("real_order_submission") is not False
        or v.get("execution_authority") is not False or v.get("research_only") is not True
        or v.get("interpolation")!="FORBIDDEN" or v.get("future_after_target")!="FORBIDDEN"):
        raise ValueError("label policy identity/authority invalid")
    hs=v.get("horizons_ms"); gaps=v.get("maximum_asof_gap_ms")
    if not isinstance(hs,list) or hs!=sorted(set(int(x) for x in hs)) or not isinstance(gaps,dict): raise ValueError("label horizons invalid")
    for h in hs:
        g=int(gaps.get(str(h),-1))
        if h<=0 or g<0 or g>h: raise ValueError("label tolerance invalid")
    return v


def validate_tape_record(v: dict[str,Any], model_sha: str|None=None) -> dict[str,Any]:
    if (v.get("schema")!=TAPE_SCHEMA or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False or v.get("real_order_submission") is not False
        or v.get("execution_authority") is not False or (model_sha and v.get("model_sha")!=model_sha)):
        raise ValueError("tape identity/authority invalid")
    raw_hash=str(v.get("record_hash") or ""); base=dict(v); base.pop("record_hash",None)
    if len(raw_hash)!=64 or raw_hash!=canonical_hash(base): raise ValueError("tape record hash invalid")
    decision=v.get("decision_wall_ns"); available=v.get("available_at_ns"); recorded=v.get("recorded_wall_ns")
    if any(type(t) is not int for t in (decision, available, recorded)):
        raise ValueError("tape clock type invalid")
    if not 0 < available <= decision <= recorded:
        raise ValueError("tape availability invalid")
    for name, length in (("model_sha",40),("policy_hash",64),("feature_schema_hash",64)):
        value=v.get(name)
        if not isinstance(value,str) or len(value)!=length or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("tape lineage invalid:"+name)
    features=v.get("features")
    if not isinstance(features,dict): raise ValueError("tape features missing")
    return v


def read_tapes(paths: list[Path]) -> list[dict[str,Any]]:
    rows=[]; seen=set(); sha=None
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip(): continue
                row=json.loads(line); sha=sha or str(row.get("model_sha") or "")
                validate_tape_record(row,sha)
                h=row["record_hash"]
                if h not in seen: rows.append(row); seen.add(h)
    rows.sort(key=lambda r:(str(r.get("market_id")),int(r["decision_wall_ns"]),str(r["record_hash"])))
    return rows


def finite(v: Any) -> float|None:
    if isinstance(v, bool): return None
    try: x=float(v)
    except (TypeError,ValueError,OverflowError): return None
    return x if math.isfinite(x) else None


def quantile(values: list[float], p: float) -> float|None:
    if not values:return None
    x=sorted(values); idx=p*(len(x)-1); lo=int(math.floor(idx)); hi=int(math.ceil(idx))
    return x[lo] if lo==hi else x[lo]*(hi-idx)+x[hi]*(idx-lo)


def label_rows(rows: list[dict[str,Any]], policy: dict[str,Any]) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    validate_policy(policy)
    groups: dict[str,list[dict[str,Any]]]={}
    cohorts=set(); identities={}; cuts={}; seen=set()
    for row in rows:
        validate_tape_record(row)
        cohorts.add(tuple(row[k] for k in ("model_sha","policy_hash","feature_schema_hash")))
        if len(cohorts)>1: raise ValueError("MIXED_EXPERIMENT_LINEAGE")
        market=str(row.get("market_id") or "")
        identity=tuple(row.get(k) for k in ("asset","horizon","yes_token","no_token","start_timestamp","end_timestamp"))
        if not market or not all(isinstance(x,str) and x for x in identity):
            raise ValueError("CONTRACT_IDENTITY_MISSING")
        if market in identities and identities[market]!=identity:
            raise ValueError("CONTRACT_IDENTITY_CHANGED_WITHIN_COHORT")
        identities[market]=identity
        cut=(market,row["decision_wall_ns"])
        if cut in cuts and cuts[cut]!=row["record_hash"]:
            raise ValueError("AMBIGUOUS_SIMULTANEOUS_FEATURE_CUT")
        cuts[cut]=row["record_hash"]
        if row["record_hash"] not in seen:
            seen.add(row["record_hash"])
            groups.setdefault(market,[]).append(row)
    output=[]; counts={str(h):0 for h in policy["horizons_ms"]}; errors={str(h):[] for h in policy["horizons_ms"]}
    status_counts={str(h):{} for h in policy["horizons_ms"]}
    for market_id, seq in groups.items():
        seq.sort(key=lambda r:int(r["decision_wall_ns"])); stamps=[int(r["decision_wall_ns"]) for r in seq]
        for i,origin in enumerate(seq):
            f0=origin["features"]; p0=finite(f0.get("pm_yes_mid")); book0=f0.get("pm_book_valid") is True
            end_ns=parse_utc_ns(origin.get("end_timestamp")); labels={}
            for h in policy["horizons_ms"]:
                key=str(h); target=int(origin["decision_wall_ns"])+int(h)*1_000_000; status="NO_ASOF_SAMPLE"; label=None
                if end_ns>0 and target>=end_ns: status="CONTRACT_EXPIRES_BEFORE_HORIZON"
                elif p0 is None or not 0 <= p0 <= 1 or not book0: status="ORIGIN_BOOK_INVALID"
                else:
                    j=bisect.bisect_right(stamps,target)-1
                    if j<=i: status="NO_LATER_ASOF_SAMPLE"
                    else:
                        future=seq[j]; gap_ms=(target-int(future["decision_wall_ns"]))/1_000_000.0
                        ff=future["features"]; p1=finite(ff.get("pm_yes_mid"))
                        if gap_ms<0: status="FUTURE_AFTER_TARGET_FORBIDDEN"
                        elif gap_ms>int(policy["maximum_asof_gap_ms"][key]): status="ASOF_GAP_TOO_LARGE"
                        elif ff.get("pm_book_valid") is not True or p1 is None or not 0 <= p1 <= 1: status="TARGET_BOOK_INVALID"
                        else:
                            label=p1-p0; status="LABELED"; counts[key]+=1; errors[key].append(gap_ms)
                status_counts[key][status]=status_counts[key].get(status,0)+1
                labels[key]={"status":status,"delta_pm_yes":label,"target_wall_ns":target,
                             "maximum_asof_gap_ms":int(policy["maximum_asof_gap_ms"][key])}
                if status=="LABELED":
                    labels[key]["asof_gap_ms"]=errors[key][-1]
                    labels[key]["label_available_wall_ns"]=int(seq[bisect.bisect_right(stamps,target)-1]["recorded_wall_ns"])
            out={"schema":OUTPUT_SCHEMA,"model_sha":origin["model_sha"],"policy_hash":origin["policy_hash"],
                 "feature_schema_hash":origin["feature_schema_hash"],"source_identity_hash":origin["source_identity_hash"],
                 "origin_record_hash":origin["record_hash"],"asset":origin.get("asset"),"horizon":origin.get("horizon"),
                 "market_id":market_id,"event_id":origin.get("event_id"),"decision_wall_ns":origin["decision_wall_ns"],
                 "available_at_ns":origin["available_at_ns"],"features":origin["features"],"source_versions":origin.get("source_versions"),
                 "labels":labels,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False}
            out["row_hash"]=canonical_hash(out); output.append(out)
    report={"schema":REPORT_SCHEMA,"paper_only":True,"execution_authority":False,"input_rows":len(rows),"output_rows":len(output),
            "markets":len(groups),"horizons_ms":policy["horizons_ms"],"labeled_counts":counts,"status_counts":status_counts,
            "asof_gap_ms":{h:{"p50":quantile(v,.5),"p95":quantile(v,.95),"max":max(v) if v else None} for h,v in errors.items()}}
    return output,report


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--tape",type=Path,action="append",required=True); ap.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_repricing_label_policy.json")); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--report",type=Path,required=True); args=ap.parse_args()
    policy=validate_policy(load_json(args.policy)); rows=read_tapes(args.tape); labeled,report=label_rows(rows,policy)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("w",encoding="utf-8") as f:
        for row in labeled:f.write(json.dumps(row,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n")
    atomic_json(args.report,report); print(json.dumps(report,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
