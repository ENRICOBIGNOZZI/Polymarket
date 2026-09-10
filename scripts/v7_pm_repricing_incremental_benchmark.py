#!/usr/bin/env python3
"""Benchmark PM microstructure vs external information for short-horizon PM repricing.

Research only. Hyperparameters are selected chronologically on whole markets.
The retrospective audit never selects a candidate. A frozen model may be refit
on all pre-forward rows only for evaluation on a NEW eight-hour forward window.
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, statistics, time
from collections import Counter
from pathlib import Path
from typing import Any

from v7_causal_book import TARGET as BOOK_TARGET
from v7_external_lead_lag_train import HORIZONS, OBS_SCHEMA, MODEL_INDEPENDENT_CAUSAL_SCHEMA, solve
from v7_external_lead_lag_collector import observation_rows, horizon_eligible
from v7_external_rich_model import FEATURE_NAMES, FEATURE_SCHEMA, logit, number
from v7_compressed_journal import journal_rows

SCHEMA = "polymarket_v7_pm_repricing_incremental_benchmark_v1"
FAMILIES = ("PM_MICRO_ONLY", "EXTERNAL_ONLY", "PM_PLUS_EXTERNAL")
RIDGES = (0.1, 1.0, 10.0, 100.0)
WINDOW_HOURS = 8
BOUNDARY_NS = 300_000_000_000
PERSISTENCE_MARGIN_BOUNDARIES = 1
PM_NAMES = (
    "pm_logit", "complement_gap_ticks",
    "yes_imbalance", "no_imbalance", "yes_ofi", "no_ofi",
    "yes_short_return_ticks", "no_short_return_ticks",
)
EXTERNAL_NAMES = (
    "return_100ms_bp", "return_250ms_bp", "microprice_shift_bp", "dispersion_bp",
    "ofi", "trade_imbalance", "binance_perp_depth_imbalance_l20",
    "binance_perp_trade_imbalance", "bybit_perp_depth_imbalance_l10",
    "deribit_perp_book_basis_bp",
)


def finite(x):
    v = number(x)
    return v if v is not None and math.isfinite(v) else None

def book_pair(row):
    cuts = row.get("origin_book_cuts")
    if not isinstance(cuts, list) or len(cuts) != 2:
        return None
    by_token = {str(x.get("token_id")): x for x in cuts if isinstance(x, dict)}
    yes, no = by_token.get(str(row.get("yes_token"))), by_token.get(str(row.get("no_token")))
    if yes is None or no is None:
        return None
    return yes, no
def pm_side(prefix, book, out):
    bid, ask, tick = finite(book.get("best_bid")), finite(book.get("best_ask")), finite(book.get("tick_size"))
    bd, ad = finite(book.get("bid_depth_l1")), finite(book.get("ask_depth_l1"))
    pf = book.get("placement_features") if isinstance(book.get("placement_features"), dict) else {}
    out[prefix+"_spread_ticks"] = ((ask-bid)/tick if None not in (bid,ask,tick) and tick > 0 else None)
    for target, source in (("imbalance","imbalance"),("ofi","ofi"),("short_return_ticks","short_return_ticks"),
                           ("ew_vol_ticks","ew_vol_ticks"),("trade_intensity","trade_intensity"),
                           ("cancel_intensity","cancel_intensity"),("aggressive_buy_ps","aggressive_buy_prints_per_second"),
                           ("aggressive_sell_ps","aggressive_sell_prints_per_second")):
        out[prefix+"_"+target] = finite(pf.get(source))
    out[prefix+"_log_bid_depth"] = math.log1p(bd) if bd is not None and bd >= 0 else None
    out[prefix+"_log_ask_depth"] = math.log1p(ad) if ad is not None and ad >= 0 else None
    micro = None
    if None not in (bid,ask,bd,ad) and bd + ad > 0:
        micro = (ask*bd + bid*ad)/(bd+ad)
    mid = 0.5*(bid+ask) if None not in (bid,ask) else None
    out[prefix+"_micro_shift_ticks"] = ((micro-mid)/tick if None not in (micro,mid,tick) and tick > 0 else None)

def pm_features(row):
    pair = book_pair(row)
    p = finite(row.get("origin_pm_yes"))
    if pair is None or p is None or not 0 < p < 1:
        return None
    yes, no = pair; out = {"pm_logit": logit(p)}
    pm_side("yes", yes, out); pm_side("no", no, out)
    yb, ya = finite(yes.get("best_bid")), finite(yes.get("best_ask"))
    nb, na = finite(no.get("best_bid")), finite(no.get("best_ask"))
    tick = max(finite(yes.get("tick_size")) or 0, finite(no.get("tick_size")) or 0)
    out["complement_gap_ticks"] = abs(0.5*(yb+ya)+0.5*(nb+na)-1)/tick if None not in (yb,ya,nb,na) and tick > 0 else None
    return out
def load_projected(path, code_sha):
    unique={}
    for row in observation_rows(journal_rows(path)):
        if (not isinstance(row,dict) or row.get("schema")!=OBS_SCHEMA
                or row.get("paper_only") is not True or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
                or row.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY"
                or row.get("target_semantics")!=BOOK_TARGET):
            continue
        source_sha=str(row.get("model_sha") or "")
        if len(source_sha)!=40 or any(c not in "0123456789abcdef" for c in source_sha): continue
        if source_sha!=code_sha and (row.get("feature_schema_version")!=FEATURE_SCHEMA
                or row.get("causal_observation_schema")!=MODEL_INDEPENDENT_CAUSAL_SCHEMA): continue
        h=int(row.get("horizon_ms") or 0); realized=finite(row.get("realized_horizon_ms")); target=finite(row.get("delta_logit"))
        if target is None or realized is None or not horizon_eligible(h,realized) or row.get("nominal_horizon_eligible") is False: continue
        pm=pm_features(row); ext=row.get("rich_model_features") if isinstance(row.get("rich_model_features"),dict) else None
        market=str(row.get("market_id") or ""); origin=str(row.get("origin_id") or ""); origin_ns=row.get("origin_observed_wall_ns")
        if pm is None or ext is None or not market or not origin or not isinstance(origin_ns,(int,float)): continue
        value={"market_id":market,"origin_id":origin,"origin_observed_wall_ns":int(origin_ns),"horizon_ms":h,
               "delta_logit":float(target),"model_sha":source_sha,"_pm":pm,
               "_ext":{name:ext.get(name) for name in EXTERNAL_NAMES}}
        key=(origin,h)
        if key in unique and unique[key]!=value: raise ValueError("incremental_benchmark:conflicting_observation")
        unique[key]=value
    return sorted(unique.values(),key=lambda r:(r["origin_observed_wall_ns"],r["market_id"],r["origin_id"],r["horizon_ms"]))


def raw_features(row, family):
    pm = row.get("_pm") if isinstance(row.get("_pm"), dict) else pm_features(row)
    ext = row.get("_ext") if isinstance(row.get("_ext"), dict) else (row.get("rich_model_features") if isinstance(row.get("rich_model_features"), dict) else None)
    if pm is None or ext is None:
        return None
    if family == "PM_MICRO_ONLY":
        return {name: pm.get(name) for name in PM_NAMES}
    if family == "EXTERNAL_ONLY":
        return {"ext__"+name: ext.get(name) for name in EXTERNAL_NAMES}
    if family == "PM_PLUS_EXTERNAL":
        return {**{"pm__"+name: pm.get(name) for name in PM_NAMES},
                **{"ext__"+name: ext.get(name) for name in EXTERNAL_NAMES}}
    raise ValueError("unknown family")

def chronological_split(rows):
    first = {}
    for r in rows:
        first[r["market_id"]] = min(first.get(r["market_id"], 10**30), int(r["origin_observed_wall_ns"]))
    markets = sorted(first, key=lambda m:(first[m],m)); n=len(markets)
    if n < 30: raise ValueError("incremental_benchmark:insufficient_markets")
    a,b=int(.6*n),int(.8*n); sets=(set(markets[:a]),set(markets[a:b]),set(markets[b:]))
    return {k:[r for r in rows if r["market_id"] in s] for k,s in zip(("train","validation","audit"),sets)}

def equal_market_weights(rows):
    counts=Counter(r["market_id"] for r in rows)
    return [1.0/counts[r["market_id"]] for r in rows]
def candidate_names(family):
    if family=="PM_MICRO_ONLY": return list(PM_NAMES)
    if family=="EXTERNAL_ONLY": return ["ext__"+x for x in EXTERNAL_NAMES]
    if family=="PM_PLUS_EXTERNAL": return ["pm__"+x for x in PM_NAMES]+["ext__"+x for x in EXTERNAL_NAMES]
    raise ValueError("unknown family")

def feature_spec(rows,family,weights):
    names=candidate_names(family); total=sum(weights); min_mass=min(total,max(5.0,.05*total))
    stats={name:[0.0,0.0,0.0] for name in names}
    for row,w in zip(rows,weights):
        raw=raw_features(row,family)
        for name in names:
            v=finite(raw.get(name))
            if v is not None:
                stats[name][0]+=w; stats[name][1]+=w*v; stats[name][2]+=w*v*v
    active=[]; means=[]; scales=[]; coverage={}; excluded={}
    for name in names:
        mass,s1,s2=stats[name]; coverage[name]=mass/total if total else 0.0
        if mass+1e-12<min_mass: excluded[name]="INSUFFICIENT_IDENTIFYING_MARKET_WEIGHT"; continue
        mean=s1/mass; var=max(0.0,s2/mass-mean*mean)
        if var<1e-16: excluded[name]="CONSTANT_ON_OBSERVED_TRAINING"; continue
        active.append(name); means.append(mean); scales.append(math.sqrt(var))
    if not active: raise ValueError("incremental_benchmark:no_features")
    return {"feature_names":active,"means":means,"scales":scales,"coverage":coverage,"excluded_features":excluded}

def design(raw,spec):
    values=[1.0]
    for name,mean,scale in zip(spec["feature_names"],spec["means"],spec["scales"]):
        v=finite(raw.get(name)); values.extend((0.0 if v is None else max(-8,min(8,(v-mean)/scale)),float(v is None)))
    return values

def prepare(rows,family):
    w=equal_market_weights(rows); spec=feature_spec(rows,family,w); n=1+2*len(spec["feature_names"])
    gram=[[0.0]*n for _ in range(n)]; rhs=[0.0]*n
    for row,ww in zip(rows,w):
        vec=design(raw_features(row,family),spec); target=float(row["delta_logit"])
        for i,a in enumerate(vec):
            rhs[i]+=ww*a*target
            for j in range(i+1): gram[i][j]+=ww*a*vec[j]
    for i in range(n):
        for j in range(i): gram[j][i]=gram[i][j]
    return spec,gram,rhs

def solve_prepared(prepared,family,ridge):
    spec,base,rhs=prepared; gram=[row[:] for row in base]
    for i in range(1,len(gram)): gram[i][i]+=ridge
    return {**spec,"coefficients":solve(gram,rhs),"ridge":ridge,"family":family}

def fit(rows,family,ridge):
    return solve_prepared(prepare(rows,family),family,ridge)

def predict(row,spec):
    raw=raw_features(row,spec["family"])
    if raw is None: return None
    return sum(a*b for a,b in zip(spec["coefficients"],design(raw,spec)))

def score(rows,spec=None):
    by_market={}
    signs={}
    for r in rows:
        pred=0.0 if spec is None else predict(r,spec)
        if pred is None: continue
        y=float(r["delta_logit"]); by_market.setdefault(r["market_id"],[]).append((pred-y)**2)
        if y != 0: signs.setdefault(r["market_id"],[]).append(float((pred>0)==(y>0)))
    mse=statistics.fmean(statistics.fmean(v) for v in by_market.values()) if by_market else None
    acc=statistics.fmean(statistics.fmean(v) for v in signs.values()) if signs else None
    return {"rows":sum(len(v) for v in by_market.values()),"markets":len(by_market),"mse":mse,"sign_accuracy":acc}

def family_horizon(rows,family):
    parts=chronological_split(rows); candidates=[]; prepared=prepare(parts["train"],family)
    for ridge in RIDGES:
        spec=solve_prepared(prepared,family,ridge)
        val=score(parts["validation"],spec); candidates.append((val["mse"],-ridge,spec))
    best=min(candidates,key=lambda x:(x[0],x[1]))[2]
    val=score(parts["validation"],best); audit=score(parts["audit"],best)
    zero_val=score(parts["validation"],None); zero_audit=score(parts["audit"],None)
    final=fit(rows,family,best["ridge"])
    return {"selected_ridge":best["ridge"],"validation":val,"audit":audit,
            "validation_zero_change":zero_val,"audit_zero_change":zero_audit,
            "audit_not_used_for_selection":True,"final_model":final}

def improvement(base,candidate):
    if base is None or candidate is None or base <= 0: return None
    return 1.0-candidate/base
def forward_window(now_ns):
    next_boundary=((now_ns//BOUNDARY_NS)+1)*BOUNDARY_NS
    start=next_boundary+PERSISTENCE_MARGIN_BOUNDARIES*BOUNDARY_NS
    return start,start+WINDOW_HOURS*3_600_000_000_000

def build(rows,code_sha):
    common=[r for r in rows if isinstance(r.get("_pm"),dict) and isinstance(r.get("_ext"),dict)]
    source_shas=sorted({str(r.get("model_sha")) for r in common})
    results={}; frozen={}
    for h in HORIZONS:
        hr=[r for r in common if int(r.get("horizon_ms") or 0)==h]
        if len({r["market_id"] for r in hr}) < 30: continue
        results[str(h)]={}; frozen[str(h)]={}
        for family in FAMILIES:
            result=family_horizon(hr,family); frozen[str(h)][family]=result.pop("final_model")
            result["audit_improvement_vs_zero_change"]=improvement(result["audit_zero_change"]["mse"],result["audit"]["mse"])
            results[str(h)][family]=result
        pm=results[str(h)]["PM_MICRO_ONLY"]["audit"]["mse"]
        for family in ("EXTERNAL_ONLY","PM_PLUS_EXTERNAL"):
            results[str(h)][family]["audit_improvement_vs_pm_micro"]=improvement(pm,results[str(h)][family]["audit"]["mse"])
    if not results: raise ValueError("incremental_benchmark:no_trainable_horizon")
    now=time.time_ns(); start,end=forward_window(now)
    dataset_hash=hashlib.sha256(json.dumps(common,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    artifact={"schema":SCHEMA,"code_sha":code_sha,"paper_only":True,"authenticated_execution":False,
              "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
              "automatic_promotion":False,"dataset_sha256":dataset_hash,"training_source_model_shas":source_shas,
              "target_semantics":BOOK_TARGET,"window_policy":"ONE_FIXED_EIGHT_HOUR_FORWARD_NO_EARLY_STOPPING",
              "generated_timestamp_ns":now,"forward_start_ns":start,"forward_end_ns":end,
              "families":list(FAMILIES),"horizons_ms":list(HORIZONS),"models":frozen}
    report={"schema":SCHEMA+"_report","artifact_identity":{k:artifact[k] for k in (
            "code_sha","dataset_sha256","forward_start_ns","forward_end_ns","target_semantics")},
            "training_rows":len(common),"training_markets":len({r["market_id"] for r in common}),"results":results}
    return artifact,report
def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n"); os.replace(tmp,path)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape",type=Path,required=True); ap.add_argument("--code-sha",required=True)
    ap.add_argument("--artifact",type=Path,required=True); ap.add_argument("--report",type=Path,required=True)
    args=ap.parse_args()
    rows=load_projected(args.tape,args.code_sha)
    artifact,report=build(rows,args.code_sha); atomic(args.artifact,artifact); atomic(args.report,report)
    print(json.dumps({"artifact":str(args.artifact),"report":str(args.report),"training_rows":report["training_rows"],
                      "training_markets":report["training_markets"],"forward_start_ns":artifact["forward_start_ns"],
                      "forward_end_ns":artifact["forward_end_ns"]},sort_keys=True))
    return 0

if __name__=="__main__": raise SystemExit(main())
