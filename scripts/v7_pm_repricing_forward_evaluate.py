#!/usr/bin/env python3
"""One-look evaluator for frozen short-horizon PM repricing artifacts."""
from __future__ import annotations
import argparse, hashlib, json, time
from pathlib import Path

from v7_pm_repricing_incremental_benchmark import (
    BOOK_TARGET, FAMILIES, HORIZONS, SCHEMA as ARTIFACT_SCHEMA,
    improvement, load_projected, score,
)

SCHEMA = "polymarket_v7_pm_repricing_forward_report_v1"
MANIFEST_SCHEMA = "polymarket_v7_pm_repricing_freeze_manifest_v1"
FINALIZATION_GRACE_NS = 5_000_000_000

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
    return h.hexdigest()

def validate_artifact(a: dict) -> None:
    if a.get("schema") != ARTIFACT_SCHEMA: raise ValueError("forward:artifact_schema")
    if a.get("paper_only") is not True or a.get("authenticated_execution") is not False: raise ValueError("forward:authority")
    if a.get("real_order_submission") is not False or a.get("automatic_promotion") is not False: raise ValueError("forward:promotion")
    if a.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY": raise ValueError("forward:execution_authority")
    if a.get("target_semantics") != BOOK_TARGET: raise ValueError("forward:target_semantics")
    if a.get("window_policy") != "ONE_FIXED_EIGHT_HOUR_FORWARD_NO_EARLY_STOPPING": raise ValueError("forward:window_policy")
    start,end=int(a.get("forward_start_ns") or 0),int(a.get("forward_end_ns") or 0)
    if start <= 0 or end-start != 8*3_600_000_000_000: raise ValueError("forward:window_length")
def validate_manifest(manifest: dict, artifact: dict, artifact_path: Path) -> None:
    if manifest.get("schema") != MANIFEST_SCHEMA: raise ValueError("forward:manifest_schema")
    if manifest.get("artifact_sha256") != sha256_file(artifact_path): raise ValueError("forward:artifact_hash")
    for key in ("code_sha","dataset_sha256","forward_start_ns","forward_end_ns"):
        if manifest.get(key) != artifact.get(key): raise ValueError("forward:manifest_identity")

def evaluate_rows(artifact: dict, rows: list[dict], now_ns: int) -> dict:
    validate_artifact(artifact)
    start,end=int(artifact["forward_start_ns"]),int(artifact["forward_end_ns"])
    unlock=end+FINALIZATION_GRACE_NS
    base={"schema":SCHEMA,"code_sha":artifact["code_sha"],"paper_only":True,
          "authenticated_execution":False,"real_order_submission":False,
          "forward_start_ns":start,"forward_end_ns":end,"evaluation_not_before_ns":unlock}
    if now_ns < unlock:
        return {**base,"state":"LOCKED_UNTIL_FIXED_END","performance_metrics":None}
    forward=[r for r in rows if start <= int(r.get("origin_observed_wall_ns") or 0) < end]
    out={}
    for horizon in HORIZONS:
        hr=[r for r in forward if int(r.get("horizon_ms") or 0)==horizon]
        zero=score(hr,None); families={}
        for family in FAMILIES:
            spec=((artifact.get("models") or {}).get(str(horizon)) or {}).get(family)
            if not isinstance(spec,dict): continue
            families[family]=score(hr,spec)
            families[family]["improvement_vs_zero_change"]=improvement(zero.get("mse"),families[family].get("mse"))
        pm=(families.get("PM_MICRO_ONLY") or {}).get("mse")
        for family in ("EXTERNAL_ONLY","PM_PLUS_EXTERNAL"):
            if family in families:
                families[family]["improvement_vs_pm_micro"]=improvement(pm,families[family].get("mse"))
        out[str(horizon)]={"zero_change":zero,"families":families}
    return {**base,"state":"FINAL_ONE_LOOK","performance_metrics":out,
            "forward_rows":len(forward),"forward_markets":len({r["market_id"] for r in forward})}
def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n")
    tmp.replace(path)

def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifact",type=Path,required=True)
    ap.add_argument("--manifest",type=Path,required=True)
    ap.add_argument("--tape",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    artifact=json.loads(args.artifact.read_text()); validate_artifact(artifact)
    manifest=json.loads(args.manifest.read_text()); validate_manifest(manifest,artifact,args.artifact)
    now=time.time_ns()
    if now < int(artifact["forward_end_ns"])+FINALIZATION_GRACE_NS:
        atomic(args.output,evaluate_rows(artifact,[],now)); return 0
    rows=load_projected(args.tape,artifact["code_sha"])
    atomic(args.output,evaluate_rows(artifact,rows,now)); return 0

if __name__=="__main__": raise SystemExit(main())
