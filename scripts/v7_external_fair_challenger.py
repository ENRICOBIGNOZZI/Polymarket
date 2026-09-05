#!/usr/bin/env python3
"""Freeze a zero-authority external residual challenger and its forward protocol.

Training uses only verified v2 forecast/settlement pairs available at freeze.
The market is a benchmark, never a feature. Publication never promotes a model.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from v7_fair_value_registry import FairModelArtifact, FairValueRegistry

MODEL_VERSION = "external-fair-structural-v7-paper"
EVIDENCE_SEMANTICS_VERSION = "external-fair-settlement-evidence-v2"
RESIDUAL_FAMILY = "btc_m5_external_residual_logit_v1"
RESIDUAL_SCHEMA = "btc-m5-external-residual-v1"
RESIDUAL_FEATURES = (
    "oracle_minus_reference_bps", "external_minus_oracle_bps",
    "external_return_1s", "external_return_5s", "terminal_window_observed_fraction",
)
PROTOCOL = {
    "schema": "polymarket_v7_frozen_economic_protocol_v1",
    "cohorts": ["market", "structural", "challenger"],
    "no_trade_benchmark_pnl": 0.0, "ridge": 1.0,
    "starting_capital_usd": 4000.0, "maximum_open_debit_usd": 10.0,
    "minimum_point_ev_per_share": 0.04, "maximum_debit_usd": 2.0,
    "execution_risk_per_share": 0.0025, "arrival_delay_ms": 250,
    "maximum_arrival_gap_ms": 2000, "maximum_book_age_ms": 1000,
    "depth_fraction": 0.375, "minimum_tte_seconds": 5.0,
    "maximum_tte_seconds": 180.0, "one_attempt_per_market_per_cohort": True,
    "decision_price_is_limit": True, "fees_at_arrival_required": True,
    "random_shuffle": False, "automatic_promotion": False,
    "paper_only": True, "real_order_submission": False,
    "execution_authority": "SHADOW_ZERO_AUTHORITY",
    "classification": "RESEARCH_REPLAY_NOT_CANONICAL_PAPER_ACCOUNT",
}


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_policy_hash(path: Path) -> str:
    return hashlib.sha256(canonical(json.loads(path.read_text())).encode()).hexdigest()


def records(paths: Iterable[Path], *, source_manifest: list | None = None,
            keep_types: set[str] | None = None, predicate: Any = None) -> dict[str, dict[str, Any]]:
    """Bounded lines, snapshot prefixes, fail-closed duplicate agreement.

    Fingerprints include unselected types; only selected payloads are retained.
    A trailing partial line is deferred, never silently treated as a record.
    """
    keep_types = keep_types or {"FORECAST", "FORECAST_FINAL"}
    output, fingerprints = {}, {}
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe_evidence_source")
        digest = hashlib.sha256(); complete_bytes = 0; lines = 0
        with path.open("rb") as handle:
            limit = os.fstat(handle.fileno()).st_size
            while handle.tell() < limit:
                raw = handle.readline(min(8 * 1024**2, limit - handle.tell()))
                if not raw.endswith(b"\n"):
                    if len(raw) == 8 * 1024**2:
                        raise ValueError("evidence_line_too_large")
                    break
                digest.update(raw); complete_bytes += len(raw); lines += 1
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict) or not row.get("record_id"):
                    raise ValueError("evidence_shape_invalid")
                identity = str(row["record_id"])
                fingerprint = hashlib.sha256(canonical(row).encode()).hexdigest()
                if identity in fingerprints and fingerprints[identity] != fingerprint:
                    raise ValueError("evidence_record_conflict:" + identity)
                fingerprints[identity] = fingerprint
                if row.get("event_type") in keep_types and (predicate is None or predicate(row)):
                    output.setdefault(identity, row)
        if source_manifest is not None:
            source_manifest.append({"path": str(path), "prefix_bytes": complete_bytes,
                                    "prefix_sha256": digest.hexdigest(), "complete_lines": lines})
    return output


def logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def origin_features(origin: dict[str, Any]) -> dict[str, float] | None:
    ext = origin.get("external_features") or {}
    oracle = finite(origin.get("oracle_value"))
    reference = finite(origin.get("reference_value"))
    spot = finite(ext.get("composite_price"))
    tte = finite(origin.get("observed_tte_seconds"))
    if not all(math.isfinite(v) and v > 0 for v in (oracle, reference, spot)) or not 0 <= tte <= 301.5:
        return None
    result = {
        "oracle_minus_reference_bps": 10000 * (oracle / reference - 1),
        "external_minus_oracle_bps": 10000 * (spot / oracle - 1),
        "external_return_1s": finite(ext.get("return_1s")),
        "external_return_5s": finite(ext.get("return_5s")),
        "terminal_window_observed_fraction": max(0.0, min(1.0, (60 - tte) / 60)),
    }
    return result if all(math.isfinite(v) for v in result.values()) else None


def settled_rows(values: Iterable[dict[str, Any]], *, policy_sha256: str,
                 cutoff_ms: int | None = None) -> list[dict[str, Any]]:
    cutoff_ms = time.time_ns() // 1000000 if cutoff_ms is None else cutoff_ms
    forecasts, finals = {}, {}
    for row in values:
        if (row.get("schema") != "polymarket_v7_external_fair_counterfactual_v1"
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
                or row.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"
                or row.get("model_version") != MODEL_VERSION
                or row.get("policy_sha256") != policy_sha256
                or row.get("evidence_semantics_version") != EVIDENCE_SEMANTICS_VERSION):
            continue
        fid = str(row.get("forecast_id") or "")
        if not fid:
            continue
        bucket = forecasts if row.get("event_type") == "FORECAST" else finals if row.get("event_type") == "FORECAST_FINAL" else None
        if bucket is not None:
            if fid in bucket and canonical(bucket[fid]) != canonical(row):
                raise ValueError("duplicate_forecast_lifecycle_conflict:" + fid)
            bucket[fid] = row
    output = []
    outcomes_by_market = {}
    for fid, final in finals.items():
        origin = forecasts.get(fid)
        if origin is None:
            continue
        observed = int(finite(origin.get("timestamp_ms"), 0))
        labeled = max(int(finite(final.get("timestamp_ms"), 0)),
                      int(finite(final.get("settlement_observed_ms"), 0)))
        p = finite(origin.get("external_only_yes"), finite(origin.get("model_yes")))
        y = finite(final.get("actual_yes")); market = str(origin.get("market_id") or "")
        rules = str(origin.get("rules_hash") or "")
        features = origin_features(origin)
        tokens = final.get("settlement_token_ids") or []
        prices = final.get("settlement_outcome_prices") or []
        yes, no = origin.get("yes_token"), origin.get("no_token")
        if (not 0 < observed < labeled < cutoff_ms or not market
                or market != str(final.get("market_id") or "")
                or origin.get("model_sha") != final.get("model_sha")
                or len(rules) != 64 or any(c not in "0123456789abcdef" for c in rules)
                or not 0 < p < 1 or y not in (0, 1) or features is None
                or origin.get("market_mid_source") != "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
                or final.get("settlement_closed") is not True
                or final.get("settlement_provider") != "POLYMARKET_GAMMA_PUBLIC"
                or not yes or not no or yes == no or len(tokens) != 2
                or set(tokens) != {yes, no} or len(prices) != 2):
            continue
        if sorted(prices) != [0.0, 1.0]:
            continue
        winner = tokens[prices.index(1.0)]
        if final.get("winning_token_id") != winner or float(winner == yes) != y:
            raise ValueError("settlement_label_mismatch:" + fid)
        if market in outcomes_by_market and outcomes_by_market[market] != y:
            raise ValueError("settlement_market_outcome_conflict:" + market)
        outcomes_by_market[market] = y
        output.append({"forecast_id": fid, "origin_record_id": origin["record_id"],
                       "final_record_id": final["record_id"], "market_id": market,
                       "rules_hash": rules, "probability": p, "actual": y,
                       "observed_ms": observed, "timestamp_ms": labeled,
                       "features": features, "yes_token": yes, "no_token": no,
                       "market_probability": finite(origin.get("market_yes"))})
    return sorted(output, key=lambda r: (r["timestamp_ms"], r["market_id"], r["forecast_id"]))


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    a = [list(row) + [value] for row, value in zip(matrix, rhs)]; n = len(rhs)
    for i in range(n):
        pivot = max(range(i, n), key=lambda k: abs(a[k][i]))
        a[i], a[pivot] = a[pivot], a[i]
        if abs(a[i][i]) < 1e-14:
            raise ValueError("singular_training_system")
        scale = a[i][i]; a[i] = [x / scale for x in a[i]]
        for j in range(n):
            if j != i:
                factor = a[j][i]; a[j] = [x - factor*y for x,y in zip(a[j],a[i])]
    return [row[-1] for row in a]


def fit_residual(rows: list[dict[str, Any]], *, ridge: float = 1.0) -> dict[str, Any]:
    if not rows or {r["actual"] for r in rows} != {0.0, 1.0}:
        raise ValueError("two_classes_required")
    counts = Counter(r["market_id"] for r in rows); weights = [1 / counts[r["market_id"]] for r in rows]
    total = sum(weights)
    means = [sum(w*r["features"][f] for w,r in zip(weights,rows))/total for f in RESIDUAL_FEATURES]
    scales = [max(1e-10, math.sqrt(sum(w*(r["features"][f]-m)**2 for w,r in zip(weights,rows))/total))
              for f,m in zip(RESIDUAL_FEATURES,means)]
    design = [[1.0, logit(r["probability"])] + [max(-8,min(8,(r["features"][f]-m)/s))
               for f,m,s in zip(RESIDUAL_FEATURES,means,scales)] for r in rows]
    prior = [0.0, 1.0] + [0.0]*len(RESIDUAL_FEATURES); beta = prior[:]; n = len(beta)
    def loss(b):
        value = 0.5*ridge*sum((x-y)**2 for x,y in zip(b,prior))
        for x,r,w in zip(design,rows,weights):
            z = sum(v*t for v,t in zip(b,x))
            value += w*(max(z,0)+math.log1p(math.exp(-abs(z)))-r["actual"]*z)
        return value
    iterations = 0
    for iterations in range(100):
        gradient = [ridge*(b-p) for b,p in zip(beta,prior)]
        hessian = [[ridge if i==j else 0.0 for j in range(n)] for i in range(n)]
        for x,r,w in zip(design,rows,weights):
            p = sigmoid(sum(b*v for b,v in zip(beta,x))); error=w*(p-r["actual"]); var=w*p*(1-p)
            for i in range(n):
                gradient[i] += error*x[i]
                for j in range(n): hessian[i][j] += var*x[i]*x[j]
        step = solve(hessian, gradient); old=loss(beta); factor=1.0
        for _ in range(30):
            candidate=[b-factor*d for b,d in zip(beta,step)]; candidate[1]=max(0.05,min(5.0,candidate[1]))
            if loss(candidate) <= old+1e-12: break
            factor *= 0.5
        if loss(candidate)>old+1e-10: raise ValueError("training_line_search_failed")
        change=max(abs(b-c) for b,c in zip(beta,candidate)); beta=candidate
        if change<1e-8: break
    return {"calibration_intercept": beta[0], "calibration_slope": beta[1],
            "feature_names": list(RESIDUAL_FEATURES), "feature_means": means,
            "feature_scales": scales, "feature_coefficients": beta[2:],
            "standardized_feature_clip": 8.0, "iterations": iterations+1,
            "training_objective": loss(beta), "ridge": ridge}


def validate_residual_parameters(artifact: FairModelArtifact) -> None:
    p=artifact.parameters
    if artifact.family != RESIDUAL_FAMILY or artifact.feature_schema_version != RESIDUAL_SCHEMA:
        raise ValueError("residual_family_or_schema")
    h=artifact.hyperparameters
    if (h.get("protocol") != PROTOCOL or h.get("automatic_promotion") is not False
            or h.get("uses_polymarket_price_as_feature") is not False
            or not artifact.training_end_ns < artifact.generated_timestamp_ns < int(h.get("forward_oos_starts_after_ns") or 0)):
        raise ValueError("residual_freeze_or_protocol_invalid")
    if p.get("feature_names") != list(RESIDUAL_FEATURES): raise ValueError("residual_feature_order")
    for key in ("feature_means","feature_scales","feature_coefficients"):
        if not isinstance(p.get(key),list) or len(p[key]) != len(RESIDUAL_FEATURES) or not all(math.isfinite(finite(v)) for v in p[key]):
            raise ValueError("residual_parameter_invalid:"+key)
    if any(s<=0 for s in p["feature_scales"]) or p.get("standardized_feature_clip") != 8.0:
        raise ValueError("residual_scale_invalid")
    if not math.isfinite(finite(p.get("calibration_intercept"))) or not 0.05 <= finite(p.get("calibration_slope")) <= 5:
        raise ValueError("residual_calibration_invalid")


def predict_residual(artifact: FairModelArtifact, structural_probability: float,
                     features: dict[str, Any]) -> float:
    validate_residual_parameters(artifact); p=artifact.parameters
    if not math.isfinite(structural_probability) or not 0 < structural_probability < 1:
        raise ValueError("structural_probability_invalid")
    z=p["calibration_intercept"]+p["calibration_slope"]*logit(structural_probability)
    for f,m,s,b in zip(RESIDUAL_FEATURES,p["feature_means"],p["feature_scales"],p["feature_coefficients"]):
        value=finite(features.get(f))
        if not math.isfinite(value): raise ValueError("missing_residual_feature:"+f)
        z += b*max(-8,min(8,(value-m)/s))
    return min(1-1e-9,max(1e-9,sigmoid(z)))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    with tmp.open("w") as h:
        h.write(json.dumps(payload,sort_keys=True,indent=2,allow_nan=False)+"\n");h.flush();os.fsync(h.fileno())
    os.replace(tmp,path)


def freeze_challenger(*, tape_paths: list[Path], registry_root: Path, config_path: Path,
                      model_sha: str, status_path: Path, minimum_contracts: int = 20) -> dict[str, Any]:
    policy_hash=canonical_policy_hash(config_path); registry=FairValueRegistry(registry_root.resolve())
    status={"schema":"polymarket_v7_external_fair_challenger_status_v1","paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,"execution_authority":"SHADOW_ZERO_AUTHORITY",
            "automatic_promotion":False,"model_sha":model_sha,"minimum_independent_settlement_markets":minimum_contracts}
    if registry.challenger_pointer.exists():
        pointer=json.loads(registry.challenger_pointer.read_text()); artifact=FairModelArtifact(**json.loads(Path(pointer["artifact"]).read_text()))
        artifact.validate(); validate_residual_parameters(artifact)
        if artifact.code_sha != model_sha or artifact.policy_version != policy_hash or artifact.model_hash != pointer.get("model_hash"):
            raise ValueError("existing_challenger_identity_drift_requires_new_run")
        status.update(state="FROZEN_CHALLENGER_REUSED",model_hash=artifact.model_hash,model_version=artifact.model_version,
                      independent_settlement_markets=artifact.training_contracts,forward_start_ns=artifact.hyperparameters["forward_oos_starts_after_ns"])
        atomic_json(status_path,status); return status
    cutoff=time.time_ns(); manifest=[]; data=records(tape_paths,source_manifest=manifest)
    rows=settled_rows(data.values(),policy_sha256=policy_hash,cutoff_ms=cutoff//1000000)
    contracts={r["market_id"] for r in rows}; status.update(settled_forecasts=len(rows),independent_settlement_markets=len(contracts))
    if len(contracts)<minimum_contracts or {r["actual"] for r in rows}!={0.0,1.0}:
        status.update(state="INSUFFICIENT_INDEPENDENT_SETTLEMENT_EVIDENCE",required_semantics=EVIDENCE_SEMANTICS_VERSION)
        atomic_json(status_path,status);return status
    parameters=fit_residual(rows,ridge=PROTOCOL["ridge"])
    payload="".join(canonical(r)+"\n" for r in rows); digest=hashlib.sha256(payload.encode()).hexdigest()
    registry.root.mkdir(parents=True,exist_ok=True); dataset=registry.root/f"training-{digest}.jsonl"
    if dataset.exists() and dataset.read_text()!=payload: raise ValueError("training_dataset_mutation")
    if not dataset.exists():
        with dataset.open("x") as h: h.write(payload);h.flush();os.fsync(h.fileno())
    published=time.time_ns(); forward=(published//300000000000+1)*300000000000
    artifact=FairModelArtifact.build(family=RESIDUAL_FAMILY,model_version="btc-m5-residual-"+digest[:16],
        feature_schema_version=RESIDUAL_SCHEMA,code_sha=model_sha,policy_version=policy_hash,artifact_role="CHALLENGER",
        training_start_ns=min(r["observed_ms"] for r in rows)*1000000,training_end_ns=max(r["timestamp_ms"] for r in rows)*1000000,
        training_contracts=len(contracts),training_days=len({r["timestamp_ms"]//86400000 for r in rows}),
        assets=("BTC",),contract_templates=("BTC_USD_UPDOWN_5M",),rules_hashes=tuple(sorted({r["rules_hash"] for r in rows})),
        parameters=parameters,hyperparameters={"training_cutoff_ns":cutoff,"forward_oos_starts_after_ns":forward,
            "training_dataset_sha256":digest,"training_dataset":str(dataset),"training_market_ids":sorted(contracts),
            "source_prefixes":manifest,"protocol":PROTOCOL,"uses_polymarket_price_as_feature":False,
            "cluster_weighting":"equal_weight_settlement_market","random_shuffle":False,"automatic_promotion":False},
        oos_scores={"state":"AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS"},
        probability_interval_diagnostics={"state":"UNVALIDATED_NO_ROBUST_EV_AUTHORITY"},
        economic_replay={"state":"AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS","execution_authority":"SHADOW_ZERO_AUTHORITY"},
        generated_timestamp_ns=published)
    registry.publish_challenger(artifact)
    status.update(state="FROZEN_CHALLENGER_PUBLISHED",model_hash=artifact.model_hash,model_version=artifact.model_version,
                  training_end_ns=artifact.training_end_ns,forward_start_ns=forward,training_dataset_sha256=digest,
                  calibration_intercept=parameters["calibration_intercept"],calibration_slope=parameters["calibration_slope"],
                  pointer=str(registry.challenger_pointer),training_source_prefixes=manifest)
    atomic_json(status_path,status); return status




def _mean(values):
    return sum(values)/len(values) if values else None


def economic_forward_report(tape_paths: list[Path], artifact: FairModelArtifact,
                            *, as_of_ms: int | None = None) -> dict[str, Any]:
    """Replay three frozen research policies on subsequent observed L2 cuts.

    These simulated decisions are NOT written to the PAPER account. Decisions
    precede arrival snapshots, missing arrival data means NONFILL, and cash is
    released only at the time a verified public settlement was observed.
    """
    artifact.validate(); validate_residual_parameters(artifact)
    now=time.time_ns()//1000000 if as_of_ms is None else as_of_ms
    start=artifact.hyperparameters["forward_oos_starts_after_ns"]//1000000
    training=set(artifact.hyperparameters["training_market_ids"])
    protocol=artifact.hyperparameters["protocol"]; sources=[]
    values=records(tape_paths,source_manifest=sources,
        keep_types={"FORECAST","FORECAST_FINAL","OPPORTUNITY_SET"},
        predicate=lambda r: (r.get("evidence_semantics_version")==EVIDENCE_SEMANTICS_VERSION
            and r.get("policy_sha256")==artifact.policy_version
            and (r.get("event_type")!="OPPORTUNITY_SET" or r.get("model_sha")==artifact.code_sha)))
    label_rows=settled_rows(values.values(),policy_sha256=artifact.policy_version,cutoff_ms=now+1)
    labels={}
    for row in label_rows:
        m=row["market_id"]
        if m not in labels or row["timestamp_ms"]<labels[m]["timestamp_ms"]:labels[m]=row
    observations=[]; exclusions=Counter()
    for row in values.values():
        if row.get("event_type")!="OPPORTUNITY_SET":continue
        if (row.get("paper_only") is not True or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
                or row.get("execution_authority")!="SHADOW_ZERO_AUTHORITY"):
            raise ValueError("unsafe_research_observation")
        timestamp=int(finite(row.get("decision_ts_ms"),0)); m=str(row.get("market_id") or "")
        c=row.get("frozen_comparison") or {}
        if timestamp>now:continue
        if timestamp<start or int(finite(row.get("reference_version"),0))*1000<start or m in training:
            exclusions["BEFORE_FORWARD_CONTRACT_OR_IN_TRAINING"]+=1;continue
        if (c.get("challenger_hash")!=artifact.model_hash
                or c.get("forward_start_ns")!=start*1000000
                or c.get("frozen_at_ns")!=artifact.generated_timestamp_ns):
            exclusions["FROZEN_PREDICTION_NOT_OBSERVED"]+=1;continue
        probabilities={"market":finite(c.get("market_probability")),
                       "structural":finite(c.get("structural_probability")),
                       "challenger":finite(c.get("challenger_probability"))}
        if not all(math.isfinite(x) and 0<=x<=1 for x in probabilities.values()):
            raise ValueError("nonfinite_frozen_comparison")
        recalculated=predict_residual(artifact,probabilities["structural"],c.get("challenger_features") or {})
        if abs(recalculated-probabilities["challenger"])>1e-10:
            raise ValueError("frozen_inference_replay_mismatch")
        if abs(probabilities["market"]-finite(row.get("market_yes")))>1e-10:
            raise ValueError("market_prediction_identity_mismatch")
        if row.get("contract_rules_hash") not in artifact.rules_hashes:
            exclusions["RULES_SCOPE_MISMATCH"]+=1;continue
        observations.append({"row":row,"timestamp":timestamp,"market":m,"probabilities":probabilities})
    observations.sort(key=lambda o:(o["timestamp"],o["row"]["record_id"]))
    cohorts={name:{"cash":protocol["starting_capital_usd"],"attempted":set(),"pending":{},
                  "positions":{},"trades":[],"nonfills":[],"scores":{},"counts":Counter()}
             for name in protocol["cohorts"]}
    trace=[]
    def fee(price,schedule):
        if not isinstance(schedule,dict) or "rate" not in schedule or "exponent" not in schedule:
            raise ValueError("UNKNOWN_FEE")
        rate,exp=finite(schedule["rate"]),finite(schedule["exponent"])
        if not math.isfinite(rate) or not math.isfinite(exp) or rate<0 or exp<0:raise ValueError("UNKNOWN_FEE")
        return rate*(price*(1-price))**exp
    def books_valid(row):
        timestamp=int(row["decision_ts_ms"]); books=row.get("books") or {}
        for side in ("YES","NO"):
            b=books.get(side) or {}; asks=b.get("asks") or []; bids=b.get("bids") or []
            receive=int(finite(b.get("receive_ts_ms"),0)); exchange=int(finite(b.get("exchange_ts_ms"),0))
            if (not asks or not bids or not b.get("token_id") or not 0<exchange<=receive<=timestamp
                    or timestamp-exchange>protocol["maximum_book_age_ms"]):return False
            if not all(isinstance(level,list) and len(level)==2 and 0<finite(level[0])<1 and finite(level[1])>0 for level in asks+bids):return False
            if asks!=sorted(asks) or bids!=sorted(bids,reverse=True) or bids[0][0]>asks[0][0]:return False
        return books['YES']['token_id']!=books['NO']['token_id']
    def settle(state,timestamp):
        for m,position in list(state["positions"].items()):
            label=labels.get(m)
            if not label or label["timestamp_ms"]>timestamp:continue
            expected=label["yes_token"] if position["outcome"]=="YES" else label["no_token"]
            if expected!=position["token_id"]:raise ValueError("research_settlement_token_mismatch")
            won=(label["actual"]==1)==(position["outcome"]=="YES")
            payout=position["shares"] if won else 0.0
            position.update(payout=payout,pnl=payout-position["entry_debit"],
                settlement_observed_ms=label["timestamp_ms"],settlement_record_id=label["final_record_id"])
            state["cash"]+=payout;del state["positions"][m]
    for obs in observations:
        row=obs["row"];ts=obs["timestamp"];m=obs["market"];books=row["books"]
        valid=books_valid(row); schedule=row.get("fee_schedule")
        for name,state in cohorts.items():
            settle(state,ts); prob=obs["probabilities"][name]
            label=labels.get(m)
            if label:
                y=label["actual"];pp=min(1-1e-12,max(1e-12,prob))
                state["scores"].setdefault(m,[]).append(((prob-y)**2,-y*math.log(pp)-(1-y)*math.log(1-pp)))
            decision={"cohort":name,"market_id":m,"observed_ms":ts,"source_record_id":row["record_id"],
                      "model_hash":artifact.model_hash,"probability":prob,"action":"ABSTAIN"}
            pending=state["pending"].get(m)
            if pending and ts>=pending["decision_ms"]+protocol["arrival_delay_ms"]:
                reason=None
                if ts-pending["decision_ms"]>protocol["maximum_arrival_gap_ms"]:reason="ARRIVAL_NOT_OBSERVED_WITHIN_LIMIT"
                elif not valid:reason="INVALID_ARRIVAL_BOOK"
                elif canonical(schedule)!=pending["fee_schedule"]:reason="FEE_SCHEDULE_CHANGED"
                elif books[pending["outcome"]]["token_id"]!=pending["token_id"]:reason="TOKEN_ID_CHANGED"
                elif finite(row.get("tte_seconds"))<protocol["minimum_tte_seconds"]:reason="CONTRACT_TOO_CLOSE_TO_END"
                chunks=[];remaining=pending["requested_shares"]
                if reason is None:
                    probability=prob if pending["outcome"]=="YES" else 1-prob
                    for price,size in books[pending["outcome"]]["asks"]:
                        if price>pending["limit_price"]+1e-12:break
                        per_fee=fee(price,schedule)
                        if probability-price-per_fee-protocol["execution_risk_per_share"]<protocol["minimum_point_ev_per_share"]:break
                        q=min(remaining,size*protocol["depth_fraction"]);remaining-=q
                        if q>0:chunks.append((q,price,q*per_fee))
                        if remaining<=1e-12:break
                    shares=sum(q for q,_,_ in chunks)
                    if shares<pending["minimum_size"]-1e-9:reason="CONSERVATIVE_MINIMUM_FILL_NOT_MET"
                if reason is not None:
                    state["nonfills"].append({**pending,"reason":reason,"arrival_ms":ts,"arrival_record_id":row["record_id"]})
                    state["counts"][reason]+=1;decision.update(action="NONFILL",reason=reason)
                else:
                    costs=sum(q*p for q,p,_ in chunks);fees=sum(f for _,_,f in chunks);debit=costs+fees
                    if debit>protocol["maximum_debit_usd"]+1e-8:raise ValueError("research_budget_exceeded")
                    trade={**pending,"arrival_ms":ts,"arrival_record_id":row["record_id"],"shares":shares,
                           "entry_cost":costs,"fees":fees,"entry_debit":debit,"fill_chunks":chunks}
                    state["cash"]-=debit;state["trades"].append(trade);state["positions"][m]=trade
                    decision.update(action="RESEARCH_SIMULATED_FILL",reason="OBSERVED_ARRIVAL_LIMIT_AND_DEPTH")
                del state["pending"][m]
            elif m not in state["attempted"]:
                tte=finite(row.get("tte_seconds")); reason="NO_POINT_EV_AFTER_COSTS"
                if not valid:reason="BOOK_NOT_FRESH_AND_COMPLETE"
                elif not protocol["minimum_tte_seconds"]<=tte<=protocol["maximum_tte_seconds"]:reason="OUTSIDE_PREDECLARED_TTE"
                else:
                    choices=[]
                    try:
                        for side,probability in (("YES",prob),("NO",1-prob)):
                            price,size=books[side]["asks"][0];per_fee=fee(price,schedule)
                            edge=probability-price-per_fee-protocol["execution_risk_per_share"]
                            choices.append((edge,side,price,per_fee,size))
                    except ValueError:reason="UNKNOWN_FEE";choices=[]
                    if choices:
                        edge,side,price,per_fee,size=max(choices,key=lambda x:(x[0],x[1]))
                        if edge>=protocol["minimum_point_ev_per_share"]:
                            minimum=finite(books[side].get("min_order_size"));q=math.floor(100*min(protocol["maximum_debit_usd"]/(price+per_fee),size*protocol["depth_fraction"]))/100
                            open_debit=sum(t["entry_debit"] for t in state["positions"].values())
                            if not math.isfinite(minimum) or minimum<=0:reason="MINIMUM_SIZE_UNKNOWN"
                            elif q<minimum:reason="BELOW_MINIMUM_SIZE"
                            elif open_debit+q*(price+per_fee)>protocol["maximum_open_debit_usd"] or q*(price+per_fee)>state["cash"]:reason="RESEARCH_BUDGET_LIMIT"
                            else:
                                state["attempted"].add(m)
                                state["pending"][m]={"market_id":m,"outcome":side,"token_id":books[side]["token_id"],
                                    "decision_ms":ts,"decision_record_id":row["record_id"],"probability_at_decision":prob,
                                    "requested_shares":q,"minimum_size":minimum,"limit_price":price,"fee_schedule":canonical(schedule)}
                                decision.update(action="RESEARCH_ORDER",reason="POINT_EV_ELIGIBLE");reason="POINT_EV_ELIGIBLE"
                state["counts"][reason]+=1;decision.setdefault("reason",reason)
            else:
                decision.update(reason="ONE_ATTEMPT_PER_CONTRACT")
            trace.append(decision)
    result={}
    for name,state in cohorts.items():
        settle(state,now)
        for m,pending in list(state["pending"].items()):
            if now-pending["decision_ms"]>protocol["maximum_arrival_gap_ms"]:
                state["nonfills"].append({**pending,"reason":"ARRIVAL_NOT_OBSERVED_WITHIN_LIMIT"});del state["pending"][m]
        terminals=[t for t in state["trades"] if "pnl" in t];realized=sum(t["pnl"] for t in terminals)
        open_debit=sum(t["entry_debit"] for t in state["positions"].values())
        result[name]={"orders":len(state["attempted"]),"fills":len(state["trades"]),"nonfills":len(state["nonfills"]),
            "pending_orders":len(state["pending"]),"terminal":len(terminals),"open_positions":len(state["positions"]),
            "realized_pnl":realized,"net_pnl_2x_fees_and_extra_execution_risk":sum(t["pnl"]-t["fees"]-t["shares"]*protocol["execution_risk_per_share"] for t in terminals),
            "cash":state["cash"],"unresolved_maximum_loss":open_debit,
            "conservative_equity_zero_mark":state["cash"],"reasons":dict(state["counts"]),
            "settled_forecast_markets":len(state["scores"]),
            "brier_equal_market_weighted":_mean([_mean([a for a,b in rows]) for rows in state["scores"].values()]),
            "log_loss_equal_market_weighted":_mean([_mean([b for a,b in rows]) for rows in state["scores"].values()]),
            "trades":state["trades"],"nonfill_evidence":state["nonfills"]}
        if abs(state["cash"]-(protocol["starting_capital_usd"]+realized-open_debit))>1e-7:
            raise ValueError("research_cash_identity_mismatch")
    return {"schema":"polymarket_v7_frozen_economic_comparison_v1","model_hash":artifact.model_hash,
        "code_sha":artifact.code_sha,"forward_start_ms":start,"as_of_ms":now,"protocol":protocol,
        "source_prefixes":sources,"observed_forward_markets":len({o['market'] for o in observations}),
        "observation_rows":len(observations),"excluded":dict(exclusions),"cohorts":result,
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "paper_account_orders_included":False,"probe_orders_included":False,
        "automatic_promotion":False,"profitability_proven":False,
        "state":"FORWARD_EVIDENCE_ACCUMULATING" if observations else "AWAITING_FORWARD_OBSERVATIONS",
        "limitations":["Snapshot-based research fills, not venue executions", "No market impact inference",
                       "Probability scores alone do not establish profitable trading", "No calibrated robust probability intervals"],
        "_decision_trace":trace}

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape",action="append",type=Path,default=[])
    parser.add_argument("--registry",type=Path,required=True);parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--model-sha",required=True);parser.add_argument("--status",type=Path,required=True)
    parser.add_argument("--minimum-contracts",type=int,default=20)
    parser.add_argument("--evaluate",action="store_true")
    args=parser.parse_args()
    if len(args.model_sha)!=40 or any(c not in "0123456789abcdef" for c in args.model_sha): raise SystemExit("exact model SHA required")
    if args.evaluate:
        pointer=json.loads((args.registry/"fair_value_challenger.json").read_text())
        artifact=FairModelArtifact(**json.loads(Path(pointer["artifact"]).read_text()))
        if artifact.code_sha != args.model_sha or artifact.policy_version != canonical_policy_hash(args.config):
            raise ValueError("evaluation_identity_mismatch")
        result=economic_forward_report(args.tape,artifact)
        trace=result.pop("_decision_trace");trace_path=args.status.with_suffix(".decisions.jsonl")
        payload="".join(canonical(r)+"\n" for r in trace)
        trace_path.parent.mkdir(parents=True,exist_ok=True)
        trace_path.write_text(payload)
        result["decision_trace"]={"path":str(trace_path),"sha256":hashlib.sha256(payload.encode()).hexdigest(),"rows":len(trace)}
        atomic_json(args.status,result)
    else:
        result=freeze_challenger(tape_paths=args.tape,registry_root=args.registry,config_path=args.config,
                                model_sha=args.model_sha,status_path=args.status,minimum_contracts=max(2,args.minimum_contracts))
    print(json.dumps(result,sort_keys=True));return 0


if __name__=="__main__":
    raise SystemExit(main())
