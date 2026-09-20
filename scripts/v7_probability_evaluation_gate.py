#!/usr/bin/env python3
"""Persist one immutable 7200-second PAPER probability evaluation cohort."""
from __future__ import annotations
import argparse, hashlib, json, os, re, time
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_probability_evaluation_gate_v1"
SHA40=re.compile(r"^[0-9a-f]{40}$")

def _read(path: Path) -> dict[str, Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ValueError("gate_json_object_required")
    return value

def _artifact_identity(path: Path, code_sha: str) -> tuple[str,dict[str,Any]]:
    raw=path.read_bytes()
    value=json.loads(raw)
    if not isinstance(value,dict): raise ValueError("probability_artifact_object")
    if (
        value.get("schema")!="v7_probability_logit_candidate_v1"
        or value.get("code_sha")!=code_sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or int(value.get("test_duration_seconds") or 0)!=7200
    ): raise ValueError("probability_artifact_identity")
    if value.get("promotion_evidence_required") is True:
        from v7_probability_promotion import validate
        validate(path, Path(str(path)+".promotion.json"), code_sha)
    return hashlib.sha256(raw).hexdigest(),value

def prepare(gate_path: Path, model_path: Path, code_sha: str,
            duration_seconds: int=7200, now_ns: int|None=None) -> dict[str,Any]:
    if not SHA40.fullmatch(code_sha): raise ValueError("exact_code_sha_required")
    if duration_seconds!=7200: raise ValueError("probability_cohort_must_be_7200_seconds")
    artifact_sha,_=_artifact_identity(model_path,code_sha)
    if gate_path.exists():
        gate=_read(gate_path)
        if (
            gate.get("schema")!=SCHEMA
            or gate.get("paper_only") is not True
            or gate.get("authenticated_execution") is not False
            or gate.get("real_order_submission") is not False
            or gate.get("code_sha")!=code_sha
            or gate.get("probability_artifact_sha256")!=artifact_sha
            or gate.get("duration_seconds")!=duration_seconds
            or not isinstance(gate.get("start_wall_ns"),int)
            or not isinstance(gate.get("end_wall_ns"),int)
            or gate["end_wall_ns"]-gate["start_wall_ns"]!=duration_seconds*1_000_000_000
        ): raise ValueError("probability_evaluation_gate_identity_mismatch")
        return gate
    start=time.time_ns() if now_ns is None else int(now_ns)
    if start<=0: raise ValueError("invalid_start_wall_ns")
    end=start+duration_seconds*1_000_000_000
    gate={
        "schema":SCHEMA,
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "real_capital_at_risk":False,
        "code_sha":code_sha,
        "probability_artifact_sha256":artifact_sha,
        "duration_seconds":duration_seconds,
        "start_wall_ns":start,
        "end_wall_ns":end,
        "restart_semantics":"REUSE_IMMUTABLE_END_NEVER_EXTEND",
        "after_end":"NO_NEW_PROBABILITY_TAKER_RISK_FEEDS_RISK_LEDGER_SETTLEMENT_CONTINUE",
    }
    gate_path.parent.mkdir(parents=True,exist_ok=True)
    tmp=gate_path.with_name(gate_path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(gate,sort_keys=True)+"\n",encoding="utf-8")
    os.chmod(tmp,0o600); os.replace(tmp,gate_path)
    return gate

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gate",type=Path,required=True)
    p.add_argument("--model",type=Path,required=True)
    p.add_argument("--code-sha",required=True)
    p.add_argument("--duration-seconds",type=int,default=7200)
    p.add_argument("--print-end-wall-ns",action="store_true")
    a=p.parse_args()
    gate=prepare(a.gate,a.model,a.code_sha,a.duration_seconds)
    print(gate["end_wall_ns"] if a.print_end_wall_ns else json.dumps(gate,sort_keys=True))
    return 0
if __name__=="__main__": raise SystemExit(main())
