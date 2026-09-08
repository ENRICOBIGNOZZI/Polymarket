#!/usr/bin/env python3
"""Explicitly freeze a short-horizon external -> PM repricing model.

No runtime training is permitted.  Candidate selection is chronological by
whole market; the final audit split is never used to select hyperparameters.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib, json, math, os
from pathlib import Path
import statistics, time
from typing import Any
from v7_external_rich_model import FEATURE_NAMES, design, number
from v7_external_lead_lag_model import SCHEMA, FAMILY, validate
from v7_external_lead_lag_collector import horizon_eligible, MAX_LABEL_DELAY_MS
from v7_causal_book import TARGET as BOOK_TARGET

OBS_SCHEMA = "polymarket_v7_external_pm_lead_lag_observation_v1"
HORIZONS = (100, 250, 500, 1000)
LEGACY_TARGET = "FIRST_OBSERVED_SNAPSHOT_AFTER_THRESHOLD"


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    a=[list(row)+[value] for row,value in zip(matrix,rhs)]; n=len(rhs)
    for i in range(n):
        pivot=max(range(i,n),key=lambda k:abs(a[k][i])); a[i],a[pivot]=a[pivot],a[i]
        if abs(a[i][i]) < 1e-14: raise ValueError("lead_lag:singular_training_system")
        scale=a[i][i]; a[i]=[x/scale for x in a[i]]
        for j in range(n):
            if j!=i:
                factor=a[j][i]; a[j]=[x-factor*y for x,y in zip(a[j],a[i])]
    return [row[-1] for row in a]


def load_rows(paths: list[Path], code_sha: str, target_semantics: str = LEGACY_TARGET) -> list[dict[str, Any]]:
    unique: dict[tuple[str,int], dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try: row=json.loads(line)
            except json.JSONDecodeError: continue
            if (not isinstance(row,dict) or row.get("schema") != OBS_SCHEMA
                    or row.get("paper_only") is not True or row.get("authenticated_execution") is not False
                    or row.get("real_order_submission") is not False or row.get("model_sha") != code_sha
                    or row.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"):
                continue
            if row.get("target_semantics", LEGACY_TARGET) != target_semantics:
                continue
            h=int(row.get("horizon_ms") or 0); origin=str(row.get("origin_id") or "")
            target=number(row.get("delta_logit")); realized=number(row.get("realized_horizon_ms"))
            features=row.get("rich_model_features") if isinstance(row.get("rich_model_features"),dict) else None
            if (not origin or target is None or realized is None or not horizon_eligible(h, realized)
                    or row.get("nominal_horizon_eligible") is False
                    or features is None or not str(row.get("market_id") or "")):
                continue
            key=(origin,h)
            if key in unique and unique[key] != row: raise ValueError("lead_lag:conflicting_observation")
            unique[key]=row
    return sorted(unique.values(), key=lambda r:(int(r["origin_observed_wall_ns"]),r["market_id"],r["origin_id"],r["horizon_ms"]))


def split(rows: list[dict[str, Any]]) -> dict[str,list[dict[str,Any]]]:
    first={}
    for r in rows: first[r["market_id"]]=min(first.get(r["market_id"],10**30),int(r["origin_observed_wall_ns"]))
    markets=sorted(first,key=lambda m:(first[m],m)); n=len(markets)
    if n < 30: raise ValueError("lead_lag:insufficient_markets")
    a,b=int(.6*n),int(.8*n); sets=(set(markets[:a]),set(markets[a:b]),set(markets[b:]))
    return {name:[r for r in rows if r["market_id"] in ss] for name,ss in zip(("train","validation","audit"),sets)}


def weights(rows: list[dict[str,Any]]) -> list[float]:
    counts=Counter(r["market_id"] for r in rows)
    return [1.0/counts[r["market_id"]] for r in rows]


def feature_spec(rows: list[dict[str,Any]], w: list[float]) -> dict[str,Any]:
    total=sum(w); names=[]; means=[]; scales=[]; coverage={}; excluded={}
    min_mass=min(total,max(5.0,.05*total))
    for name in FEATURE_NAMES:
        good=[(number(r["rich_model_features"].get(name)),ww) for r,ww in zip(rows,w)
              if number(r["rich_model_features"].get(name)) is not None]
        mass=sum(ww for _,ww in good); coverage[name]=mass/total if total else 0.0
        if mass+1e-12 < min_mass: excluded[name]="INSUFFICIENT_IDENTIFYING_MARKET_WEIGHT"; continue
        mean=sum(x*ww for x,ww in good)/mass
        var=sum(ww*(x-mean)**2 for x,ww in good)/mass
        if var < 1e-16: excluded[name]="CONSTANT_ON_OBSERVED_TRAINING"; continue
        names.append(name); means.append(mean); scales.append(math.sqrt(var))
    if not names: raise ValueError("lead_lag:no_features")
    return {"feature_names":names,"means":means,"scales":scales,"coverage":coverage,"excluded_features":excluded}


def fit(rows: list[dict[str,Any]], ridge: float) -> dict[str,Any]:
    w=weights(rows); spec=feature_spec(rows,w); x=[design(r["rich_model_features"],spec) for r in rows]
    y=[float(r["delta_logit"]) for r in rows]; width=len(x[0])
    gram=[[0.0]*width for _ in range(width)]; rhs=[0.0]*width
    for vec,target,ww in zip(x,y,w):
        for i,a in enumerate(vec):
            rhs[i]+=ww*a*target
            for j in range(i+1): gram[i][j]+=ww*a*vec[j]
    for i in range(width):
        for j in range(i): gram[j][i]=gram[i][j]
        if i>0: gram[i][i]+=ridge
    coeff=solve(gram,rhs)
    return {**spec,"coefficients":coeff,"ridge":ridge,"maximum_absolute_delta_logit":2.0}


def score(rows: list[dict[str,Any]], spec: dict[str,Any] | None) -> dict[str,Any]:
    by_market={}
    for r in rows:
        pred=0.0 if spec is None else sum(a*b for a,b in zip(spec["coefficients"],design(r["rich_model_features"],spec)))
        err=(pred-float(r["delta_logit"]))**2
        by_market.setdefault(r["market_id"],[]).append(err)
    mse=statistics.fmean(statistics.fmean(v) for v in by_market.values()) if by_market else None
    return {"rows":len(rows),"markets":len(by_market),"mse":mse}


def train(rows: list[dict[str,Any]], code_sha: str) -> tuple[dict[str,Any],dict[str,Any]]:
    semantics = {r.get("target_semantics", LEGACY_TARGET) for r in rows}
    if len(semantics) != 1 or not semantics <= {LEGACY_TARGET, BOOK_TARGET}:
        raise ValueError("lead_lag:mixed_or_unknown_target_semantics")
    parts=split(rows); models={}; report={}
    for horizon in HORIZONS:
        hp={k:[r for r in v if int(r["horizon_ms"])==horizon] for k,v in parts.items()}
        if any(len({r["market_id"] for r in v}) < 6 for v in hp.values()):
            continue
        candidates=[]
        for ridge in (.1,1.0,10.0,100.0):
            spec=fit(hp["train"],ridge); candidates.append((score(hp["validation"],spec)["mse"],ridge,spec))
        _,_,best=min(candidates,key=lambda x:(x[0],-x[1]))
        val=score(hp["validation"],best); base=score(hp["validation"],None); audit=score(hp["audit"],best)
        models[str(horizon)]={**best,"validation":val,"validation_zero_change_baseline":base,
                              "retrospective_audit":audit,"audit_not_used_for_selection":True,
                              "validation_improves_zero_change":bool(val["mse"] < base["mse"])}
        report[str(horizon)]={"validation":val,"baseline":base,"audit":audit,"ridge":best["ridge"],
                              "active_features":best["feature_names"],"excluded_features":best["excluded_features"]}
    if not models: raise ValueError("lead_lag:no_trainable_horizon")
    now=time.time_ns(); boundary=((now//1_000_000_000//300)+1)*300*1_000_000_000
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    model={"schema":SCHEMA,"family":FAMILY,"model_sha":code_sha,"paper_only":True,
           "authenticated_execution":False,"real_order_submission":False,"research_only":True,
           "execution_authority":"ZERO_AUTHORITY_SIGNAL_ONLY","training_lifecycle":"EXPLICIT_FROZEN_ARTIFACT_ONLY",
           "generated_timestamp_ns":now,"forward_oos_starts_after_ns":boundary,"dataset_sha256":digest,
           "training_markets":len({r["market_id"] for r in rows}),"models":models,
           "maximum_label_delay_ms":MAX_LABEL_DELAY_MS,
           "target_semantics":next(iter(semantics))}
    validate(model)
    return model,{"schema":"polymarket_v7_external_pm_lead_lag_training_report_v1","model_sha":code_sha,
                  "paper_only":True,"research_only":True,"dataset_sha256":digest,"horizons":report,
                  "forward_oos_starts_after_ns":boundary}


def atomic(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n"); os.replace(tmp,path)


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--tape",action="append",type=Path,required=True)
    ap.add_argument("--model-sha",required=True); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--report",type=Path,required=True)
    ap.add_argument("--target-semantics", choices=(LEGACY_TARGET, BOOK_TARGET), default=LEGACY_TARGET)
    args=ap.parse_args(); rows=load_rows(args.tape,args.model_sha,args.target_semantics); model,report=train(rows,args.model_sha); atomic(args.output,model); atomic(args.report,report); return 0
if __name__=="__main__": raise SystemExit(main())
