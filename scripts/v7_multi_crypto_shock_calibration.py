#!/usr/bin/env python3
"""Training-only shock distribution calibration for six-asset SHADOW research."""
from __future__ import annotations
import argparse,hashlib,json,math,os
from pathlib import Path
from typing import Any
from v7_multi_crypto_repricing_labeler import read_tapes

SCHEMA="polymarket_v7_multi_crypto_shock_calibration_policy_v1"
REPORT_SCHEMA="polymarket_v7_multi_crypto_shock_calibration_report_v1"
ASSETS=("BTC","ETH","SOL","XRP","DOGE","BNB")

def load_json(path:Path)->dict[str,Any]:
    v=json.loads(path.read_text());
    if not isinstance(v,dict):raise ValueError("object required")
    return v

def atomic_json(path:Path,v:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}"); tmp.write_text(json.dumps(v,indent=2,sort_keys=True,allow_nan=False)+"\n"); os.replace(tmp,path)

def validate_policy(v:dict[str,Any])->dict[str,Any]:
    if (v.get("schema")!=SCHEMA or v.get("paper_only") is not True or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False or v.get("execution_authority") is not False or v.get("research_only") is not True
        or v.get("selected_threshold") is not None or v.get("selection_policy")!="NO_THRESHOLD_SELECTION_FROM_CURRENT_FORWARD"):
        raise ValueError("shock_calibration_policy_identity")
    qs=v.get("absolute_z_quantiles"); sf=float(v.get("sigma_floor_quantile") or -1)
    if not isinstance(qs,list) or qs!=sorted(set(float(x) for x in qs)) or any(not 0<q<1 for q in qs) or not 0<sf<1:raise ValueError("shock_calibration_quantiles")
    if int(v.get("shock_window_ms") or 0)!=100 or int(v.get("minimum_shock_observations") or 0)<2:raise ValueError("shock_calibration_window")
    return v

def quantile(values:list[float],q:float)->float|None:
    if not values:return None
    x=sorted(values); pos=q*(len(x)-1); lo=int(math.floor(pos)); hi=int(math.ceil(pos)); return x[lo] if lo==hi else x[lo]*(hi-pos)+x[hi]*(pos-lo)

def finite(v:Any)->float|None:
    try:x=float(v)
    except (TypeError,ValueError,OverflowError):return None
    return x if math.isfinite(x) else None

def dedup_shocks(rows:list[dict[str,Any]],policy:dict[str,Any])->list[dict[str,Any]]:
    out=[]; seen=set(); minimum=int(policy["minimum_shock_observations"])
    for row in rows:
        asset=str(row.get("asset") or ""); f=row.get("features") if isinstance(row.get("features"),dict) else {}; ext=f.get("external") if isinstance(f.get("external"),dict) else {}; shock=ext.get("shock") if isinstance(ext.get("shock"),dict) else {}
        z=finite(shock.get("shock_z_unfloored")); sigma=finite(shock.get("sigma_100ms_bp_prior")); obs=int(shock.get("observations") or 0); ret=finite(shock.get("return_100ms_bp")); decision=int(row.get("decision_wall_ns") or 0)
        source=row.get("source_versions") if isinstance(row.get("source_versions"),dict) else {}; state=int(source.get("external_state_version") or 0)
        if asset not in ASSETS or z is None or sigma is None or sigma<=0 or ret is None or obs<minimum or decision<=0 or state<=0:continue
        key=(asset,state)
        if key in seen:continue
        seen.add(key); out.append({"asset":asset,"decision_wall_ns":decision,"external_state_version":state,"z":z,"abs_z":abs(z),"sigma_bp":sigma,"return_bp":ret})
    out.sort(key=lambda r:(r["decision_wall_ns"],r["asset"],r["external_state_version"])); return out

def split_training(rows:list[dict[str,Any]],policy:dict[str,Any])->tuple[list[dict[str,Any]],list[int]]:
    cluster_ns=int(policy["cluster_seconds"])*1_000_000_000; clusters=sorted({r["decision_wall_ns"]//cluster_ns for r in rows}); ntrain=max(1,int(len(clusters)*float(policy["training_cluster_fraction"]))) if clusters else 0; train=set(clusters[:ntrain]); return [r for r in rows if r["decision_wall_ns"]//cluster_ns in train],clusters

def calibrate(rows:list[dict[str,Any]],policy:dict[str,Any])->dict[str,Any]:
    unique=dedup_shocks(rows,policy); training,clusters=split_training(unique,policy)
    report={"schema":REPORT_SCHEMA,"version":1,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,"research_only":True,
            "selected_threshold":None,"input_feature_rows":len(rows),"unique_shock_rows":len(unique),"time_clusters":len(clusters),"training_rows":len(training),"training_clusters":len({r["decision_wall_ns"]//(int(policy["cluster_seconds"])*1_000_000_000) for r in training}),"candidate_quantiles":{},"sigma_floor_candidates_bp":{},"counts_by_asset":{},"status":"INSUFFICIENT_EVIDENCE","blockers":[]}
    for asset in ASSETS:report["counts_by_asset"][asset]=sum(r["asset"]==asset for r in unique)
    if len(clusters)<int(policy["minimum_time_clusters"]):report["blockers"]=["INSUFFICIENT_INDEPENDENT_TIME_CLUSTERS"];return report
    qs=[float(q) for q in policy["absolute_z_quantiles"]]; pooled=[r["abs_z"] for r in training]
    report["candidate_quantiles"]["COMMON"]={str(q):quantile(pooled,q) for q in qs}
    for asset in ASSETS:
        asset_rows=[r for r in training if r["asset"]==asset]; report["candidate_quantiles"][asset]={str(q):quantile([r["abs_z"] for r in asset_rows],q) for q in qs}
        report["sigma_floor_candidates_bp"][asset]=quantile([r["sigma_bp"] for r in asset_rows],float(policy["sigma_floor_quantile"]))
    report["status"]="TRAINING_DISTRIBUTION_CALIBRATED";return report

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--feature-tape",type=Path,action="append",required=True); ap.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_shock_calibration.json")); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--code-sha",required=True); args=ap.parse_args()
    if len(args.code_sha)!=40 or any(ch not in "0123456789abcdef" for ch in args.code_sha):raise ValueError("exact code SHA required")
    policy=validate_policy(load_json(args.policy)); rows=read_tapes(args.feature_tape); report=calibrate(rows,policy); report["code_sha"]=args.code_sha; report["feature_tape_sha256"]={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in args.feature_tape}; atomic_json(args.output,report); print(json.dumps(report,sort_keys=True)); return 0
if __name__=="__main__":raise SystemExit(main())
