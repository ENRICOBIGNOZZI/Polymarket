#!/usr/bin/env python3
"""Causal, zero-authority horse race for PAPER Maker cancel overlays."""
from __future__ import annotations

import argparse, gzip, hashlib, json, math, pathlib, random
from collections import defaultdict
from typing import Any, Iterable

SCHEMA = "polymarket_v7_maker_execution_horse_race_v1"
SEMANTICS = "maker-paper-v7.2-bilateral-inventory"
SHADOW_SCHEMA = "polymarket_v7_pm_repricing_shadow_v1"


def num(x: Any, default: float = math.nan) -> float:
    try: y = float(x)
    except (TypeError, ValueError, OverflowError): return default
    return y if math.isfinite(y) else default


def stable(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def rid(row: dict[str, Any]) -> str:
    return str(row.get("record_id") or hashlib.sha256(stable(row).encode()).hexdigest())


def files(paths: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    out: set[pathlib.Path] = set()
    for raw in paths:
        p = pathlib.Path(raw)
        if p.is_file(): out.add(p.resolve()); continue
        if p.exists():
            for pat in ("*.json", "*.jsonl", "*.jsonl.gz"):
                out.update(q.resolve() for q in p.rglob(pat) if q.is_file())
    return sorted(out)


def records(paths: Iterable[pathlib.Path]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for p in files(paths):
        try:
            if p.suffix == ".json":
                raw = json.loads(p.read_text(encoding="utf-8")); vals = raw if isinstance(raw, list) else [raw]
            else:
                vals = []
                opener = gzip.open if p.name.endswith(".gz") else open
                with opener(p, "rt", encoding="utf-8") as h:
                    for line in h:
                        try: vals.append(json.loads(line))
                        except json.JSONDecodeError: pass
            for row in vals:
                if not isinstance(row, dict): continue
                key = rid(row)
                if key in seen: continue
                seen.add(key); out.append(row)
        except (OSError, json.JSONDecodeError): pass
    return out


def meta(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") if isinstance(row.get("metadata"), dict) else {}


def life(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("model_sha") or "unknown"), str(row.get("order_id") or "")


def placement(order: dict[str, Any]) -> str:
    m = meta(order); explicit = m.get("placement_action") or order.get("placement_action")
    if explicit: return str(explicit).upper()
    action = str(order.get("intended_action") or m.get("action") or "UNKNOWN").upper()
    if action != "MAKE": return action
    env = m.get("opportunity_envelope") if isinstance(m.get("opportunity_envelope"), dict) else {}
    p = {str(r)[10:] for r in env.get("reasons", []) if str(r).startswith("PLACEMENT_")}
    return p.pop() if len(p) == 1 else "UNKNOWN"


def outcome(order: dict[str, Any]) -> str:
    return str(meta(order).get("outcome") or order.get("outcome") or "UNKNOWN").upper()


def scope(order: dict[str, Any]) -> tuple[str, str]:
    m = meta(order); env = m.get("opportunity_envelope") if isinstance(m.get("opportunity_envelope"), dict) else {}
    c = env.get("crypto_context") if isinstance(env.get("crypto_context"), dict) else {}
    return str(c.get("asset") or m.get("asset") or "").upper(), str(c.get("horizon") or m.get("horizon") or "").upper()


def build_fills(rows: list[dict[str, Any]], *, markout_horizon: str, action: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("event_type") != "ORDER_SUBMITTED" or not r.get("order_id"): continue
        m = meta(r); sem = str(m.get("execution_semantics_version") or "")
        if m.get("component") not in {None, "professional_maker"} or (sem and sem != SEMANTICS): continue
        if str(r.get("side") or m.get("execution_side") or "").upper() != "BUY": continue
        if placement(r) != action or outcome(r) not in {"YES", "NO"} or int(num(r.get("recorded_ts_ms"), 0)) <= 0: continue
        orders[life(r)] = r
    fills: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    exact: dict[tuple[str, str, str], tuple[int, float]] = {}; fallback: defaultdict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        k = life(r)
        if k not in orders: continue
        if r.get("event_type") == "FILL" and int(num(r.get("recorded_ts_ms"), 0)) > 0 and num(r.get("filled_size"), 0) > 0:
            fills[k].append(r)
        if r.get("event_type") == "MARKOUT" and isinstance(r.get("markouts"), dict) and markout_horizon in r["markouts"]:
            value, ts = num(r["markouts"][markout_horizon]), int(num(r.get("recorded_ts_ms"), 0))
            if not math.isfinite(value) or ts <= 0: continue
            fid = str(r.get("fill_id") or "")
            if fid:
                kk = (k[0], k[1], fid)
                if kk not in exact or ts < exact[kk][0]: exact[kk] = (ts, value)
            else: fallback[k].append((ts, value))
    out, d = [], {"candidate_orders":len(orders),"fills_seen":0,"fills_with_markout":0,"fills_missing_markout":0,"fills_invalid_timing":0}
    for k, fs in fills.items():
        o = orders[k]; start = int(num(o.get("recorded_ts_ms"), 0)); market = str(o.get("market_id") or ""); asset, hor = scope(o)
        fb = sorted(fallback[k]); one = fb[0][1] if len(fs) == 1 and fb else math.nan
        for f in sorted(fs, key=lambda x:int(num(x.get("recorded_ts_ms"),0))):
            d["fills_seen"] += 1; t = int(num(f.get("recorded_ts_ms"),0)); fid = str(f.get("fill_id") or "")
            if t <= start: d["fills_invalid_timing"] += 1; continue
            mark = exact.get((k[0],k[1],fid)); mv = mark[1] if mark else one
            if not math.isfinite(mv): d["fills_missing_markout"] += 1; continue
            sh = num(f.get("filled_size"),0); d["fills_with_markout"] += 1
            out.append({"source_model_sha":k[0],"order_id":k[1],"fill_id":fid,"market_id":market,"event_cluster":str(o.get("event_id") or market or "UNKNOWN"),"asset":asset,"horizon":hor,"outcome":outcome(o),"order_start_ns":start*1_000_000,"fill_ns":t*1_000_000,"filled_shares":sh,"markout_per_share":mv,"baseline_markout_dollars":mv*sh})
    out.sort(key=lambda x:(x["fill_ns"],x["source_model_sha"],x["order_id"],x["fill_id"]))
    return out, d


def hard_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out=[]
    for r in rows:
        oc=str(r.get("stale_buy_outcome") or r.get("outcome") or "").upper(); av=int(num(r.get("available_ns",r.get("publish_wall_ns",r.get("trigger_receive_wall_ns"))),0))
        if oc not in {"YES","NO"} or av<=0 or r.get("valid") is False: continue
        out.append({"available_ns":av,"market_id":str(r.get("market_id") or ""),"asset":str(r.get("asset") or "").upper(),"horizon":str(r.get("horizon") or "").upper(),"outcome":oc,"signal_id":str(r.get("signal_id") or r.get("signal_version") or rid(r))})
    return sorted(out,key=lambda x:x["available_ns"])


def learned_events(rows: list[dict[str, Any]], *, family: str, horizon_ms: int, threshold: float) -> list[dict[str, Any]]:
    out=[]
    for r in rows:
        if r.get("schema")!=SHADOW_SCHEMA or r.get("paper_only") is not True or r.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY": continue
        if str(r.get("family") or "")!=family or int(num(r.get("horizon_ms"),0))!=horizon_ms or abs(num(r.get("threshold_ticks"))-threshold)>1e-12: continue
        av=int(num(r.get("scored_wall_ns"),0)); market=str(r.get("market_id") or ""); sid=str(r.get("origin_id") or rid(r))
        if av<=0 or not market: continue
        if r.get("would_veto_yes_buy") is True: out.append({"available_ns":av,"market_id":market,"outcome":"YES","signal_id":sid})
        if r.get("would_veto_no_buy") is True: out.append({"available_ns":av,"market_id":market,"outcome":"NO","signal_id":sid})
    return sorted(out,key=lambda x:x["available_ns"])


def matches(fill: dict[str, Any], event: dict[str, Any], *, global_hard: bool) -> bool:
    market=str(event.get("market_id") or "")
    if market: return market==fill["market_id"]
    return global_hard and bool(event.get("asset") and event.get("horizon") and event["asset"]==fill["asset"] and event["horizon"]==fill["horizon"])


def first_cancel(fill: dict[str, Any], events: list[dict[str, Any]], latency_ns: int, *, global_hard: bool=False) -> dict[str, Any]|None:
    start, stop=int(fill["order_start_ns"]), int(fill["fill_ns"])
    for e in events:
        av=int(e["available_ns"])
        if av<start: continue
        if av>=stop: break
        if e["outcome"]!=fill["outcome"] or not matches(fill,e,global_hard=global_hard): continue
        if av+latency_ns<stop: return {**e,"effective_cancel_ns":av+latency_ns}
    return None


def evaluate(fills: list[dict[str, Any]], events: list[dict[str, Any]], latency_ns: int, *, global_hard: bool=False) -> tuple[dict[str,Any],list[dict[str,Any]]]:
    rows=[]
    for f in fills:
        e=first_cancel(f,events,latency_ns,global_hard=global_hard); avoided=e is not None
        rows.append({**f,"avoided":avoided,"improvement_dollars":-f["baseline_markout_dollars"] if avoided else 0.0})
    avoided=[r for r in rows if r["avoided"]]; shares=sum(r["filled_shares"] for r in fills); ash=sum(r["filled_shares"] for r in avoided); net=sum(r["improvement_dollars"] for r in rows)
    return {"eligible_fills":len(fills),"eligible_shares":shares,"baseline_markout_dollars":sum(r["baseline_markout_dollars"] for r in fills),"avoided_fills":len(avoided),"avoided_shares":ash,"fill_share_removed_fraction":ash/shares if shares else None,"avoided_adverse_markout_dollars":sum(max(0.0,-r["baseline_markout_dollars"]) for r in avoided),"forgone_favorable_markout_dollars":sum(max(0.0,r["baseline_markout_dollars"]) for r in avoided),"net_markout_improvement_dollars":net,"net_improvement_per_baseline_share":net/shares if shares else None,"net_improvement_per_avoided_share":net/ash if ash else None},rows


def quantile(v:list[float],p:float)->float|None:
    if not v:return None
    s=sorted(v); x=p*(len(s)-1); lo,hi=math.floor(x),math.ceil(x)
    return s[lo] if lo==hi else s[lo]*(hi-x)+s[hi]*(x-lo)


def inference(rows:list[dict[str,Any]], n:int, seed:int)->dict[str,Any]:
    g:defaultdict[str,list[dict[str,Any]]]=defaultdict(list)
    for r in rows:g[str(r["event_cluster"])].append(r)
    stats={k:(sum(x["improvement_dollars"] for x in v),sum(x["filled_shares"] for x in v)) for k,v in g.items()}; names=sorted(stats); rng=random.Random(seed); boot=[]
    for _ in range(n):
        pick=[rng.choice(names) for _ in names] if names else []; den=sum(stats[k][1] for k in pick)
        if den:boot.append(sum(stats[k][0] for k in pick)/den)
    best=max(names,key=lambda k:stats[k][0],default=None); loo=None
    if best is not None and len(names)>1:
        den=sum(v[1] for k,v in stats.items() if k!=best); loo=sum(v[0] for k,v in stats.items() if k!=best)/den if den else None
    return {"event_clusters":len(names),"bootstrap_samples":n,"cluster_bootstrap_ci95_per_baseline_share":[quantile(boot,.025),quantile(boot,.975)],"leave_best_cluster_out_per_baseline_share":loo,"best_cluster":best}


def race(fills:list[dict[str,Any]], hard:list[dict[str,Any]], learned:list[dict[str,Any]], *, latency_ms:float, bootstrap:int, seed:int, global_hard:bool)->dict[str,Any]:
    ns=int(round(latency_ms*1e6)); b,br=evaluate(fills,[],ns); h,hr=evaluate(fills,hard,ns,global_hard=global_hard); l,lr=evaluate(fills,learned,ns)
    anchor=[{k:r[k] for k in ("source_model_sha","order_id","fill_id","market_id","outcome","order_start_ns","fill_ns","filled_shares","markout_per_share")} for r in fills]
    return {"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","automatic_promotion":False,"counterfactual_scope":"SAME_LIVE_QUOTE_CANCEL_ONLY_PRE_ORDER_SIGNALS_IGNORED","strict_causality":"effective_cancel_ns < observed_fill_ns","cancel_latency_ms":latency_ms,"fill_anchor_sha256":hashlib.sha256(stable(anchor).encode()).hexdigest(),"hard_event_sha256":hashlib.sha256(stable(hard).encode()).hexdigest(),"learned_event_sha256":hashlib.sha256(stable(learned).encode()).hexdigest(),"eligible_fill_observations":len(fills),"hard_cancel_events":len(hard),"learned_veto_events":len(learned),"both_policies_avoid_same_fill_count":sum(hr[i]["avoided"] and lr[i]["avoided"] for i in range(len(fills))),"results":{"BASELINE":{**b,**inference(br,bootstrap,seed)},"HARD_EXTERNAL_CANCEL":{**h,**inference(hr,bootstrap,seed+1)},"LEARNED_PM_EXTERNAL_250MS":{**l,**inference(lr,bootstrap,seed+2)}}}


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--maker-evidence",type=pathlib.Path,action="append",required=True); ap.add_argument("--hard-cancel-events",type=pathlib.Path,action="append",default=[]); ap.add_argument("--learned-shadow",type=pathlib.Path,action="append",default=[]); ap.add_argument("--output",type=pathlib.Path,required=True); ap.add_argument("--markout-horizon",default="1s"); ap.add_argument("--placement-action",default="JOIN"); ap.add_argument("--cancel-latency-ms",type=float,default=25.0); ap.add_argument("--allow-global-hard-scope",action="store_true"); ap.add_argument("--learned-family",default="PM_PLUS_EXTERNAL"); ap.add_argument("--learned-horizon-ms",type=int,default=250); ap.add_argument("--learned-threshold-ticks",type=float,default=1.0); ap.add_argument("--bootstrap-samples",type=int,default=5000); ap.add_argument("--stress-cancel-latency-ms",default="5,25,50,100,200"); ap.add_argument("--seed",type=int,default=1729); a=ap.parse_args()
    if not math.isfinite(a.cancel_latency_ms) or a.cancel_latency_ms<0 or a.bootstrap_samples<0: raise SystemExit("invalid horse-race arguments")
    fills,diag=build_fills(records(a.maker_evidence),markout_horizon=str(a.markout_horizon),action=str(a.placement_action).upper())
    if not fills:raise SystemExit("horse_race:no_fill_conditioned_markout_observations")
    hard=hard_events(records(a.hard_cancel_events)); learned=learned_events(records(a.learned_shadow),family=a.learned_family,horizon_ms=a.learned_horizon_ms,threshold=a.learned_threshold_ticks)
    result=race(fills,hard,learned,latency_ms=a.cancel_latency_ms,bootstrap=a.bootstrap_samples,seed=a.seed,global_hard=a.allow_global_hard_scope); stress=[]
    for token in str(a.stress_cancel_latency_ms).split(","):
        if not token.strip():continue
        x=num(token.strip())
        if not math.isfinite(x) or x<0:raise SystemExit("invalid --stress-cancel-latency-ms")
        q=race(fills,hard,learned,latency_ms=x,bootstrap=0,seed=a.seed,global_hard=a.allow_global_hard_scope)
        stress.append({"cancel_latency_ms":x,"hard_external_cancel_net_per_baseline_share":q["results"]["HARD_EXTERNAL_CANCEL"]["net_improvement_per_baseline_share"],"hard_external_cancel_fill_share_removed_fraction":q["results"]["HARD_EXTERNAL_CANCEL"]["fill_share_removed_fraction"],"learned_250ms_net_per_baseline_share":q["results"]["LEARNED_PM_EXTERNAL_250MS"]["net_improvement_per_baseline_share"],"learned_250ms_fill_share_removed_fraction":q["results"]["LEARNED_PM_EXTERNAL_250MS"]["fill_share_removed_fraction"]})
    result.update({"markout_horizon":str(a.markout_horizon),"placement_action":str(a.placement_action).upper(),"maker_diagnostics":diag,"latency_stress":sorted(stress,key=lambda x:x["cancel_latency_ms"]),"learned_model_contract":{"family":a.learned_family,"horizon_ms":a.learned_horizon_ms,"threshold_ticks":a.learned_threshold_ticks,"threshold_role":"DIAGNOSTIC_ONLY_UNTIL_FROZEN_ON_PRE_FORWARD_OR_VALIDATION_DATA"}}); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8"); print(json.dumps(result,indent=2,sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())
