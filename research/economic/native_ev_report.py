#!/usr/bin/env python3
"""Causal OOS evaluation for the native price-aware settlement EV model."""
from __future__ import annotations
import argparse,json,math
from pathlib import Path
from typing import Any
from models import fit_settlement_residual,predict,settlement_edge

SCHEMA="polymarket_v7_native_ev_report_v1"
DATASET_SCHEMA="polymarket_v7_native_ev_dataset_v1"
FEATURES=[
 "signal_strength_bp","confirmation_aligned_bp","tte_seconds","signal_age_ms",
 "spread","depth_imbalance","direction_up","technical_signal_valid","confirmed_non_opposing",
 *[f"asset_{x}" for x in ("BTC","ETH","SOL","XRP","DOGE","BNB")],
 *[f"horizon_{x}" for x in ("M5","M15","H1","H4","D1")],
]
BUFFERS=(0.0,0.0025,0.005,0.01,0.02)

def load(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get("schema")!=DATASET_SCHEMA:raise ValueError("dataset schema")
    return value

def _metrics(y:list[int],p:list[float])->dict[str,float|None]:
    if not y:return {"n":0,"brier":None,"log_loss":None}
    eps=1e-12
    return {"n":len(y),
      "brier":sum((a-b)**2 for a,b in zip(y,p))/len(y),
      "log_loss":sum(-a*math.log(max(eps,min(1-eps,b)))-(1-a)*math.log(max(eps,min(1-eps,1-b))) for a,b in zip(y,p))/len(y)}

def _gate(rows:list[dict],probabilities:list[float],buffer:float)->dict[str,Any]:
    pnl=[];selected=[]
    for row,p in zip(rows,probabilities):
        edge=settlement_edge(p,float(row["executable_ask"]),float(row["fee_per_share"]),buffer)
        if edge<=0:continue
        value=float(row["ex_post_net_per_share"]); pnl.append(value); selected.append(row)
    return {"buffer":buffer,"trades":len(pnl),"net_per_share_sum":sum(pnl),
            "net_per_share_mean":sum(pnl)/len(pnl) if pnl else None,
            "win_rate":sum(x>0 for x in pnl)/len(pnl) if pnl else None,
            "max_loss_per_share":min(pnl) if pnl else None,
            "markets":[r["market"] for r in selected]}

def evaluate(dataset:dict[str,Any],minimum_training_markets:int=100,minimum_test_markets:int=20)->dict[str,Any]:
    rows=sorted(dataset.get("rows") or [],key=lambda r:(int(r["decision_ns"]),r["market"]))
    report={"schema":SCHEMA,"paper_only":True,"execution_authority":False,"automatic_promotion":False,
            "dataset_sha256":dataset.get("dataset_sha256"),"total_markets":len(rows),
            "minimum_training_markets":minimum_training_markets,"minimum_test_markets":minimum_test_markets,
            "state":"INSUFFICIENT_EVIDENCE","blockers":[]}
    if len(rows)<max(minimum_training_markets+minimum_test_markets,3):
        report["blockers"]=["INSUFFICIENT_UNIQUE_MARKETS"];return report
    c1=int(rows[int(.60*len(rows))]["decision_ns"]); c2=int(rows[int(.80*len(rows))]["decision_ns"])
    train=[r for r in rows if int(r["decision_ns"])<c1 and int(r["label_observed_ns"])<c1]
    purged=[r for r in rows if int(r["decision_ns"])<c1 and int(r["label_observed_ns"])>=c1]
    validation=[r for r in rows if c1<=int(r["decision_ns"])<c2]
    test=[r for r in rows if int(r["decision_ns"])>=c2]
    report["split"]={"train":len(train),"purged":len(purged),"validation":len(validation),"test":len(test),
                     "train_cutoff_ns":c1,"test_cutoff_ns":c2}
    blockers=[]
    if len(train)<minimum_training_markets:blockers.append("INSUFFICIENT_CAUSAL_TRAINING_MARKETS")
    if len(test)<minimum_test_markets:blockers.append("INSUFFICIENT_HELDOUT_TEST_MARKETS")
    if blockers:report["blockers"]=blockers;return report
    model=fit_settlement_residual(train,feature_names=FEATURES,train_end_ns=c1,
                                  dataset_sha256=str(dataset["dataset_sha256"]),ridge=1.0,
                                  minimum_markets=minimum_training_markets)
    vp=predict(model,validation); tp=predict(model,test)
    vy=[int(r["outcome"]) for r in validation];ty=[int(r["outcome"]) for r in test]
    vbase=[float(r["pm_probability"]) for r in validation];tbase=[float(r["pm_probability"]) for r in test]
    report["model"]={k:v for k,v in model.items() if k!="coefficients"}
    report["validation"]={"model":_metrics(vy,vp),"pm_prior":_metrics(vy,vbase),
                          "gates":[_gate(validation,vp,b) for b in BUFFERS]}
    report["test"]={"model":_metrics(ty,tp),"pm_prior":_metrics(ty,tbase),
                    "gates":[_gate(test,tp,b) for b in BUFFERS]}
    report["state"]="HELDOUT_EVALUATED_NO_AUTO_PROMOTION"
    return report

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--dataset",type=Path,required=True);ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    if args.output.exists():raise SystemExit("output exists")
    report=evaluate(load(args.dataset));args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)+"\n")
    print(json.dumps({"state":report["state"],"blockers":report["blockers"],"total_markets":report["total_markets"]},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
