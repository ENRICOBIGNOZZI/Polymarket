#!/usr/bin/env python3
"""Chronological, leakage-safe ablation research for multi-crypto PM repricing."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

ROW_SCHEMA = "polymarket_v7_multi_crypto_compact_repricing_labeled_row_v1"
REPORT_SCHEMA = "polymarket_v7_multi_crypto_repricing_research_report_v1"
POLICY_SCHEMA = "polymarket_v7_multi_crypto_repricing_research_policy_v1"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
HORIZONS = ("M5", "M15")

PM_FEATURES = ("pm_yes_mid", "pm_yes_spread", "pm_yes_imbalance", "pm_complete_set_gap")
OWN_FEATURES = ("ext_return_50ms_bp", "ext_return_100ms_bp", "ext_return_250ms_bp", "ext_return_1s_bp",
                "shock_z", "dispersion_bps", "aggregate_ofi", "aggregate_trade_imbalance", "fresh_venue_count")
ORACLE_FEATURES = ("tte_seconds", "distance_to_reference_bp", "spot_minus_oracle_bp")
DERIVATIVE_FEATURES = tuple(f"{venue}_{field}" for venue in ("DERIBIT", "BYBIT_LINEAR", "BINANCE_USDM")
                            for field in ("basis_bp", "funding_rate"))
CROSS_FEATURES = tuple(f"leader_{leader}_{field}" for leader in ("BTC", "ETH")
                       for field in ("return_100ms_bp", "shock_z"))
ABLATIONS = {
    "PM_MICRO": PM_FEATURES,
    "OWN_SHOCK": PM_FEATURES + OWN_FEATURES,
    "ORACLE": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES,
    "DERIVATIVES": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES + DERIVATIVE_FEATURES,
    "FULL_CROSS": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES + DERIVATIVE_FEATURES + CROSS_FEATURES,
}


def canonical_hash_bytes(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ValueError(f"{path}: object required")
    return value


def atomic_json(path: Path,value: dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8"); os.replace(tmp,path)


def finite(value: Any) -> float|None:
    try:x=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return x if math.isfinite(x) else None


def validate_policy(value: dict[str,Any]) -> dict[str,Any]:
    if (value.get("schema")!=POLICY_SCHEMA or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False or value.get("real_order_submission") is not False
            or value.get("execution_authority") is not False or value.get("research_only") is not True
            or value.get("ridge_tuning")!="FORBIDDEN_IN_FORWARD"
            or value.get("missing_policy")!="TRAINING_MEDIAN_PLUS_MISSING_INDICATOR"
            or value.get("scaling_policy")!="TRAINING_ONLY_STANDARDIZATION"):
        raise ValueError("research_policy_identity_or_authority")
    fractions=value.get("split_fractions") or {}; total=sum(float(fractions.get(k,0)) for k in ("train","validation","test"))
    if abs(total-1.0)>1e-12 or any(float(fractions.get(k,0))<=0 for k in ("train","validation","test")):
        raise ValueError("research_split_fractions")
    if int(value.get("primary_horizon_ms") or 0)<=0 or int(value.get("maximum_label_horizon_ms") or 0)<int(value["primary_horizon_ms"]):
        raise ValueError("research_horizon")
    if float(value.get("ridge_lambda") or -1)<0 or int(value.get("cluster_seconds") or 0)<=0:
        raise ValueError("research_model_policy")
    return value


def validate_label_report(value: dict[str,Any]) -> dict[str,Any]:
    if (value.get("schema")!="polymarket_v7_multi_crypto_compact_repricing_label_report_v1"
            or value.get("paper_only") is not True or value.get("execution_authority") is not False):
        raise ValueError("label_report_identity_or_authority")
    for name in ("input_model_sha","labeler_code_sha"):
        raw=str(value.get(name) or "")
        if len(raw)!=40 or any(ch not in "0123456789abcdef" for ch in raw): raise ValueError("label_report_sha")
    return value


def read_labeled(path: Path, label_report: dict[str,Any], primary_horizon_ms: int) -> list[dict[str,Any]]:
    rows=[]; producer=str(label_report["input_model_sha"]); key=str(primary_horizon_ms)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip(): continue
            row=json.loads(line)
            if (not isinstance(row,dict) or row.get("schema")!=ROW_SCHEMA or row.get("paper_only") is not True
                    or row.get("authenticated_execution") is not False or row.get("real_order_submission") is not False
                    or row.get("execution_authority") is not False or row.get("model_sha")!=producer):
                raise ValueError("labeled_row_identity_or_authority")
            labels=row.get("labels") if isinstance(row.get("labels"),dict) else {}
            label=labels.get(key) if isinstance(labels.get(key),dict) else {}
            if label.get("status")!="LABELED": continue
            y=finite(label.get("delta_pm_yes")); decision=int(row.get("decision_wall_ns") or 0)
            if y is None or decision<=0: raise ValueError("labeled_row_target")
            row=dict(row); row["_target"]=y; rows.append(row)
    return rows


def flatten(row: dict[str,Any]) -> dict[str,float|None]:
    f=row.get("features") if isinstance(row.get("features"),dict) else {}; ext=f.get("external") if isinstance(f.get("external"),dict) else {}
    shock=ext.get("shock") if isinstance(ext.get("shock"),dict) else {}; leaders=f.get("leader_features") if isinstance(f.get("leader_features"),dict) else {}
    out={"pm_yes_mid":finite(f.get("pm_yes_mid")),"pm_yes_spread":finite(f.get("pm_yes_spread")),"pm_yes_imbalance":finite(f.get("pm_yes_imbalance")),
         "pm_complete_set_gap":finite(f.get("pm_complete_set_gap")),"tte_seconds":finite(f.get("tte_seconds")),"distance_to_reference_bp":finite(f.get("distance_to_reference_bp")),
         "spot_minus_oracle_bp":finite(f.get("spot_minus_oracle_bp")),"ext_return_50ms_bp":finite(ext.get("return_50ms_bp")),"ext_return_100ms_bp":finite(ext.get("return_100ms_bp")),
         "ext_return_250ms_bp":finite(ext.get("return_250ms_bp")),"ext_return_1s_bp":finite(ext.get("return_1s_bp")),"shock_z":finite(shock.get("shock_z_unfloored")),
         "dispersion_bps":finite(ext.get("dispersion_bps")),"aggregate_ofi":finite(ext.get("aggregate_ofi")),"aggregate_trade_imbalance":finite(ext.get("aggregate_trade_imbalance")),
         "fresh_venue_count":finite(ext.get("fresh_venue_count"))}
    derivatives=f.get("derivatives") if isinstance(f.get("derivatives"),list) else []
    for venue in ("DERIBIT","BYBIT_LINEAR","BINANCE_USDM"):
        item=next((d for d in derivatives if isinstance(d,dict) and d.get("venue")==venue and d.get("usable") is True),{})
        out[f"{venue}_basis_bp"]=finite(item.get("basis_to_spot_bp")); out[f"{venue}_funding_rate"]=finite(item.get("funding_rate"))
    for leader in ("BTC","ETH"):
        item=leaders.get(leader) if isinstance(leaders.get(leader),dict) else {}
        out[f"leader_{leader}_return_100ms_bp"]=finite(item.get("return_100ms_bp")); out[f"leader_{leader}_shock_z"]=finite(item.get("shock_z_unfloored"))
    return out


def median(values:list[float])->float:
    if not values:return 0.0
    ordered=sorted(values); n=len(ordered); return ordered[n//2] if n%2 else .5*(ordered[n//2-1]+ordered[n//2])


class Transformer:
    def __init__(self,names:tuple[str,...]): self.names=names; self.stats:dict[str,tuple[float,float,float]]={}
    def fit(self,rows:list[dict[str,Any]])->None:
        flattened=[flatten(r) for r in rows]
        for name in self.names:
            observed=[float(x[name]) for x in flattened if x.get(name) is not None]
            med=median(observed); imputed=[med if x.get(name) is None else float(x[name]) for x in flattened]
            mean=sum(imputed)/len(imputed) if imputed else 0.0
            var=sum((v-mean)**2 for v in imputed)/len(imputed) if imputed else 0.0; scale=math.sqrt(var)
            if not math.isfinite(scale) or scale<1e-12: scale=1.0
            self.stats[name]=(med,mean,scale)
    def transform(self,row:dict[str,Any])->list[float]:
        x=flatten(row); values=[1.0]
        for name in self.names:
            med,mean,scale=self.stats[name]; raw=x.get(name); missing=raw is None; value=med if missing else float(raw)
            values.append((value-mean)/scale); values.append(1.0 if missing else 0.0)
        asset=str(row.get("asset") or ""); horizon=str(row.get("horizon") or "")
        values.extend(1.0 if asset==a else 0.0 for a in ASSETS[1:]); values.append(1.0 if horizon=="M15" else 0.0)
        return values


def solve_linear(matrix:list[list[float]], vector:list[float])->list[float]:
    n=len(vector); a=[list(matrix[i])+[float(vector[i])] for i in range(n)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda r:abs(a[r][col]))
        if abs(a[pivot][col])<1e-14: raise ValueError("ridge_singular")
        a[col],a[pivot]=a[pivot],a[col]; scale=a[col][col]
        for j in range(col,n+1):a[col][j]/=scale
        for r in range(n):
            if r==col:continue
            factor=a[r][col]
            if factor==0:continue
            for j in range(col,n+1):a[r][j]-=factor*a[col][j]
    return [a[i][n] for i in range(n)]


def fit_ridge(rows:list[dict[str,Any]], transformer:Transformer, ridge_lambda:float)->list[float]:
    design=[transformer.transform(r) for r in rows]; target=[float(r["_target"]) for r in rows]
    p=len(design[0]); gram=[[0.0]*p for _ in range(p)]; rhs=[0.0]*p
    for x,y in zip(design,target):
        for j in range(p):
            rhs[j]+=x[j]*y
            for k in range(j,p): gram[j][k]+=x[j]*x[k]
    for j in range(p):
        for k in range(j):gram[j][k]=gram[k][j]
        if j>0:gram[j][j]+=ridge_lambda
    return solve_linear(gram,rhs)


def predict(row:dict[str,Any],transformer:Transformer,beta:list[float])->float:
    x=transformer.transform(row); return sum(a*b for a,b in zip(x,beta))


def split_rows(rows:list[dict[str,Any]],policy:dict[str,Any])->dict[str,Any]:
    cluster_ns=int(policy["cluster_seconds"])*1_000_000_000; max_h_ns=int(policy["maximum_label_horizon_ms"])*1_000_000
    clusters=sorted({int(r["decision_wall_ns"])//cluster_ns for r in rows}); n=len(clusters); f=policy["split_fractions"]
    n_train=max(1,int(n*float(f["train"]))); n_val=max(1,int(n*float(f["validation"])))
    if n_train+n_val>=n: n_val=max(1,n-n_train-1)
    train_clusters=set(clusters[:n_train]); val_clusters=set(clusters[n_train:n_train+n_val]); test_clusters=set(clusters[n_train+n_val:])
    val_start=min(val_clusters)*cluster_ns if val_clusters else None; test_start=min(test_clusters)*cluster_ns if test_clusters else None
    def keep_train(r):
        d=int(r["decision_wall_ns"]); return d//cluster_ns in train_clusters and (val_start is None or d+max_h_ns<val_start)
    def keep_val(r):
        d=int(r["decision_wall_ns"]); return d//cluster_ns in val_clusters and (val_start is None or d>=val_start+max_h_ns) and (test_start is None or d+max_h_ns<test_start)
    def keep_test(r):
        d=int(r["decision_wall_ns"]); return d//cluster_ns in test_clusters and (test_start is None or d>=test_start+max_h_ns)
    return {"clusters":clusters,"train_clusters":sorted(train_clusters),"validation_clusters":sorted(val_clusters),"test_clusters":sorted(test_clusters),
            "train":[r for r in rows if keep_train(r)],"validation":[r for r in rows if keep_val(r)],"test":[r for r in rows if keep_test(r)],
            "cluster_seconds":int(policy["cluster_seconds"]),"purge_embargo_ms":int(policy["maximum_label_horizon_ms"])}


def correlation(x:list[float],y:list[float])->float|None:
    if len(x)<2:return None
    mx=sum(x)/len(x); my=sum(y)/len(y); vx=sum((v-mx)**2 for v in x); vy=sum((v-my)**2 for v in y)
    if vx<=0 or vy<=0:return None
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/math.sqrt(vx*vy)


def metrics(rows:list[dict[str,Any]], predictions:list[float])->dict[str,Any]:
    if not rows:return {"n":0,"mse":None,"mae":None,"correlation":None,"direction_accuracy_nonzero":None,"mean_target":None}
    y=[float(r["_target"]) for r in rows]; errors=[p-t for p,t in zip(predictions,y)]; nonzero=[i for i,t in enumerate(y) if abs(t)>1e-12]
    return {"n":len(rows),"mse":sum(e*e for e in errors)/len(errors),"mae":sum(abs(e) for e in errors)/len(errors),
            "correlation":correlation(predictions,y),"direction_accuracy_nonzero":(sum((predictions[i]>0)==(y[i]>0) for i in nonzero)/len(nonzero) if nonzero else None),
            "mean_target":sum(y)/len(y),"target_std":math.sqrt(sum((t-sum(y)/len(y))**2 for t in y)/len(y))}


def group_counts(rows:list[dict[str,Any]])->dict[str,int]:
    output:dict[str,int]={}
    for r in rows:
        key=f"{r.get('asset')}/{r.get('horizon')}"; output[key]=output.get(key,0)+1
    return dict(sorted(output.items()))


def evaluate(rows:list[dict[str,Any]],policy:dict[str,Any],lineage:dict[str,Any])->dict[str,Any]:
    split=split_rows(rows,policy); clusters=split["clusters"]
    report={"schema":REPORT_SCHEMA,"version":1,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,"research_only":True,
            "economic_evidence":False,"primary_horizon_ms":int(policy["primary_horizon_ms"]),"input_labeled_rows":len(rows),"time_clusters":len(clusters),
            "split":{"cluster_seconds":split["cluster_seconds"],"purge_embargo_ms":split["purge_embargo_ms"],"train_clusters":len(split["train_clusters"]),"validation_clusters":len(split["validation_clusters"]),"test_clusters":len(split["test_clusters"]),
                     "train_rows":len(split["train"]),"validation_rows":len(split["validation"]),"test_rows":len(split["test"])},
            "counts_by_asset_horizon":group_counts(rows),"lineage":lineage,"models":{},"limitations":["mid-price repricing only; not executable PnL","ridge lambda fixed and not tuned on this forward sample"]}
    if len(clusters)<int(policy["minimum_time_clusters"]) or len(split["test_clusters"])<int(policy["minimum_test_clusters"]):
        report["status"]="INSUFFICIENT_EVIDENCE"; report["blockers"]=["INSUFFICIENT_INDEPENDENT_TIME_CLUSTERS"]; return report
    if min(len(split[k]) for k in ("train","validation","test"))<10:
        report["status"]="INSUFFICIENT_EVIDENCE"; report["blockers"]=["INSUFFICIENT_ROWS_AFTER_PURGE_EMBARGO"]; return report
    baseline={part:metrics(split[part],[0.0]*len(split[part])) for part in ("train","validation","test")}
    report["models"]["ZERO_REPRICING"]={"feature_count":0,"metrics":baseline}
    lam=float(policy["ridge_lambda"])
    for name,features in ABLATIONS.items():
        transformer=Transformer(features); transformer.fit(split["train"]); beta=fit_ridge(split["train"],transformer,lam)
        model_metrics={part:metrics(split[part],[predict(r,transformer,beta) for r in split[part]]) for part in ("train","validation","test")}
        report["models"][name]={"feature_names":list(features),"expanded_design_columns":len(beta),"ridge_lambda":lam,"metrics":model_metrics,
                                "training_transform":{feature:{"median":v[0],"mean":v[1],"scale":v[2]} for feature,v in transformer.stats.items()}}
    report["status"]="RESEARCH_OOS_AVAILABLE"; report["blockers"]=[]; return report


def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--labeled",type=Path,required=True); parser.add_argument("--label-report",type=Path,required=True)
    parser.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_repricing_research.json")); parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); policy=validate_policy(load_json(args.policy)); label_report=validate_label_report(load_json(args.label_report))
    rows=read_labeled(args.labeled,label_report,int(policy["primary_horizon_ms"]))
    lineage={"labeled_file":str(args.labeled),"labeled_sha256":canonical_hash_bytes(args.labeled),"label_report_file":str(args.label_report),
             "label_report_sha256":canonical_hash_bytes(args.label_report),"producer_model_sha":label_report["input_model_sha"],"labeler_code_sha":label_report["labeler_code_sha"]}
    report=evaluate(rows,policy,lineage); atomic_json(args.output,report); print(json.dumps(report,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
