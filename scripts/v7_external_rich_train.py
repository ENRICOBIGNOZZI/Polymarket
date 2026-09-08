#!/usr/bin/env python3
"""Freeze rich external-information ML for bounded live PAPER research.

Training uses verified public settlements, original receive-time feature cuts,
whole-market temporal splits and label-availability embargoes. Candidate choice
uses validation only; the final retrospective audit is not a selection set.
Publication never creates a CHAMPION or mature economic authority.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any

from v7_external_fair_challenger import records, solve
from v7_external_rich_model import (
    FAMILY, FEATURE_SCHEMA, FEATURE_NAMES, MODEL_PREFIX, design, features,
    logit, number, predict, sigmoid, validate_parameters,
)
from v7_fair_value_registry import FairModelArtifact, FairValueRegistry, canonical_hash
from v7_external_economic_common import atomic_json

SCHEMA = "polymarket_v7_rich_external_training_v1"


def build_rows(values: list[dict[str, Any]], cutoff_ms: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    origins: dict[str, dict[str, Any]] = {}
    finals: dict[str, dict[str, Any]] = {}
    excluded: Counter = Counter()
    for r in values:
        if (r.get("schema") != "polymarket_v7_external_fair_counterfactual_v1"
                or r.get("evidence_semantics_version") != "external-fair-settlement-evidence-v2"
                or r.get("paper_only") is not True or r.get("authenticated_execution") is not False
                or r.get("real_order_submission") is not False
                or r.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"):
            excluded["INCOMPATIBLE_EVIDENCE"] += 1
            continue
        fid = str(r.get("forecast_id") or "")
        if not fid:
            continue
        bucket = origins if r.get("event_type") == "FORECAST" else finals if r.get("event_type") == "FORECAST_FINAL" else None
        if bucket is not None:
            if fid in bucket and bucket[fid] != r:
                raise ValueError("rich_train:conflicting_forecast_lifecycle:" + fid)
            bucket[fid] = r
    out, outcomes = [], {}
    for fid, o in origins.items():
        f = finals.get(fid)
        if f is None:
            excluded["PENDING_SETTLEMENT"] += 1
            continue
        observed = number(o.get("observed_ms", o.get("timestamp_ms")))
        label = max(number(f.get("timestamp_ms")) or 0, number(f.get("settlement_observed_ms")) or 0)
        start = number(o.get("reference_version"))
        pm = number(o.get("market_yes")); y = number(f.get("actual_yes"))
        mid = str(o.get("market_id") or ""); rules = str(o.get("rules_hash") or "")
        tokens = f.get("settlement_token_ids") or []; prices = f.get("settlement_outcome_prices") or []
        yes, no = o.get("yes_token"), o.get("no_token")
        if (observed is None or start is None or not 0 < start * 1000 <= observed < label < cutoff_ms
                or label < (start + 300) * 1000 or not mid or mid != str(f.get("market_id") or "")
                or o.get("model_sha") != f.get("model_sha")
                or o.get("policy_sha256") != f.get("policy_sha256")
                or len(rules) != 64 or any(c not in "0123456789abcdef" for c in rules)
                or pm is None or not 0 <= pm <= 1 or y not in (0.0, 1.0)
                or o.get("market_mid_source") != "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
                or f.get("settlement_closed") is not True
                or f.get("settlement_provider") != "POLYMARKET_GAMMA_PUBLIC"
                or not yes or not no or yes == no or len(tokens) != 2 or set(tokens) != {yes, no}
                or len(prices) != 2 or sorted(prices) != [0.0, 1.0]):
            excluded["UNVERIFIED_OR_NOT_YET_AVAILABLE_LABEL"] += 1
            continue
        winner = tokens[prices.index(1.0)]
        if winner != f.get("winning_token_id") or float(winner == yes) != y:
            raise ValueError("rich_train:verified_label_mismatch")
        if mid in outcomes and outcomes[mid] != y:
            raise ValueError("rich_train:market_outcome_conflict")
        outcomes[mid] = y
        try:
            cut = o.get("rich_feature_cut")
            if cut is not None:
                if not isinstance(cut, dict) or canonical_hash(cut) != o.get("rich_feature_sha256"):
                    raise ValueError("rich_train:feature_cut_hash_mismatch")
                cut_ns = number(cut.get("observed_wall_ns"))
                if cut_ns is None or not start * 1e9 <= cut_ns < (observed + 1) * 1e6:
                    raise ValueError("rich_train:future_or_wrong_market_feature_cut")
                pm = number(cut.get("market_probability"))
                if pm is None or not 0 <= pm <= 1:
                    raise ValueError("rich_train:feature_cut_market_probability")
                raw = features(cut)
            else:
                raw = features(o)
        except ValueError as exc:
            excluded[str(exc)] += 1
            continue
        out.append({"market_id": mid, "forecast_id": fid, "market_start_ms": int(start * 1000),
                    "observed_ms": int(observed), "label_received_ms": int(label),
                    "actual": y, "market_probability": pm, "features": raw,
                    "rules_hash": rules, "origin_record_id": o.get("record_id"),
                    "final_record_id": f.get("record_id"), "source_code_sha": o.get("model_sha")})
    return sorted(out, key=lambda r: (r["market_start_ms"], r["observed_ms"], r["forecast_id"])), dict(excluded)


def split_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    starts = {}
    for r in rows:
        if r["market_id"] in starts and starts[r["market_id"]] != r["market_start_ms"]:
            raise ValueError("rich_train:market_start_conflict")
        starts[r["market_id"]] = r["market_start_ms"]
    markets = sorted(starts, key=lambda m: (starts[m], m)); n = len(markets)
    if n < 30:
        raise ValueError("rich_train:insufficient_markets")
    a, b = int(n * .60), int(n * .80)
    tr, va, au = set(markets[:a]), set(markets[a:b]), set(markets[b:])
    validation_start, audit_start = starts[markets[a]], starts[markets[b]]
    # Labels, not just forecast times, must have been available before the next
    # partition starts. Overlapping contracts are embargoed, not shuffled.
    return {
        "train": [r for r in rows if r["market_id"] in tr and r["label_received_ms"] < validation_start],
        "validation": [r for r in rows if r["market_id"] in va and r["label_received_ms"] < audit_start],
        "audit": [r for r in rows if r["market_id"] in au],
    }


def cluster_weights(rows: list[dict[str, Any]]) -> list[float]:
    counts = Counter(r["market_id"] for r in rows)
    return [1 / counts[r["market_id"]] for r in rows]


def fit(rows: list[dict[str, Any]], ridge: float, offset: str) -> dict[str, Any]:
    if ridge <= 0 or offset not in ("market", "none") or {r["actual"] for r in rows} != {0.0, 1.0}:
        raise ValueError("rich_train:fit_contract")
    weights = cluster_weights(rows); total = sum(weights)
    names, means, scales, excluded = [], [], [], {}
    for name in FEATURE_NAMES:
        good = [(number(r["features"].get(name)), w) for r, w in zip(rows, weights)
                if number(r["features"].get(name)) is not None]
        mass = sum(w for _, w in good)
        if mass < .90 * total:
            excluded[name] = "LESS_THAN_90_PERCENT_MARKET_WEIGHT_COVERAGE"
            continue
        mean = sum(x * w for x, w in good) / mass
        variance = sum(w * (x - mean) ** 2 for x, w in good) / mass
        if variance < 1e-16:
            excluded[name] = "CONSTANT_ON_TRAINING"
            continue
        names.append(name); means.append(mean); scales.append(math.sqrt(variance))
    if not names:
        raise ValueError("rich_train:no_informative_features")
    p = {"feature_names": names, "means": means, "scales": scales, "offset": offset,
         "missing_policy": "TRAIN_MEAN_AND_EXPLICIT_INDICATOR", "ridge": ridge,
         "excluded_features": excluded}
    x = [design(r["features"], p) for r in rows]
    offsets = [logit(r["market_probability"]) if offset == "market" else 0.0 for r in rows]
    width = len(x[0]); beta = [0.0] * width
    def objective(coeff):
        value = .5 * ridge * sum(v*v for v in coeff)
        for row, vec, off, w in zip(rows, x, offsets, weights):
            z = off + sum(a*b for a,b in zip(coeff, vec))
            value += w * (max(z, 0) + math.log1p(math.exp(-abs(z))) - row["actual"] * z)
        return value
    converged = False
    for iteration in range(60):
        gradient = [ridge * v for v in beta]
        hessian = [[ridge if i == j else 0.0 for j in range(width)] for i in range(width)]
        for row, vec, off, w in zip(rows, x, offsets, weights):
            probability = sigmoid(off + sum(a*b for a,b in zip(beta, vec)))
            error, var = w * (probability-row["actual"]), w * probability * (1-probability)
            for i in range(width):
                gradient[i] += error * vec[i]
                if vec[i]:
                    for j in range(i+1):
                        hessian[i][j] += var * vec[i] * vec[j]
        for i in range(width):
            for j in range(i):
                hessian[j][i] = hessian[i][j]
        step = solve(hessian, gradient); before = objective(beta); factor = 1.0
        for _ in range(25):
            candidate = [v-factor*d for v,d in zip(beta, step)]
            if objective(candidate) <= before + 1e-12:
                break
            factor *= .5
        else:
            raise ValueError("rich_train:line_search_failed")
        beta = candidate
        if max(abs(factor*d) for d in step) < 1e-7:
            converged = True
            break
    if not converged or any(not math.isfinite(v) for v in beta):
        raise ValueError("rich_train:not_converged")
    p.update(coefficients=beta, iterations=iteration+1, converged=True)
    return p


def score(rows: list[dict[str, Any]], parameters: dict[str, Any] | None) -> dict[str, Any]:
    brier, loss = defaultdict(list), defaultdict(list)
    for row in rows:
        pm = row["market_probability"]
        probability = pm if parameters is None else sigmoid(
            (logit(pm) if parameters["offset"] == "market" else 0.0)
            + sum(a*b for a,b in zip(parameters["coefficients"], design(row["features"], parameters))))
        probability = min(1-1e-9, max(1e-9, probability)); y = row["actual"]
        brier[row["market_id"]].append((probability-y)**2)
        loss[row["market_id"]].append(-y*math.log(probability)-(1-y)*math.log(1-probability))
    return {"rows": len(rows), "contracts": len(brier),
            "brier": statistics.fmean(statistics.fmean(v) for v in brier.values()) if brier else None,
            "log_loss": statistics.fmean(statistics.fmean(v) for v in loss.values()) if loss else None}


def train(rows: list[dict[str, Any]], code_sha: str, policy_hash: str,
          source_manifest: list[dict[str, Any]], generated_ns: int | None = None) -> tuple[Any, dict[str, Any]]:
    parts = split_rows(rows)
    if any(len({r["market_id"] for r in values}) < 8 for values in parts.values()):
        raise ValueError("rich_train:insufficient_temporal_partitions")
    candidates = []
    for offset in ("none", "market"):
        for ridge in (1.0, 10.0, 100.0):
            p = fit(parts["train"], ridge, offset)
            candidates.append({"parameters": p, "validation": score(parts["validation"], p)})
    best = min(candidates, key=lambda c: (c["validation"]["brier"], c["validation"]["log_loss"],
                                         c["parameters"]["offset"] != "market", -c["parameters"]["ridge"]))
    generated_ns = time.time_ns() if generated_ns is None else generated_ns
    if max(r["label_received_ms"] for r in rows)*1_000_000 >= generated_ns:
        raise ValueError("rich_train:future_training_label")
    boundary_ns = ((generated_ns // 1_000_000_000 // 300) + 1) * 300 * 1_000_000_000
    dataset_hash = canonical_hash({"rows": rows})
    audit = score(parts["audit"], best["parameters"])
    used = parts["train"] + parts["validation"]
    artifact = FairModelArtifact.build(
        family=FAMILY, model_version=MODEL_PREFIX+dataset_hash[:16],
        feature_schema_version=FEATURE_SCHEMA, code_sha=code_sha, policy_version=policy_hash,
        artifact_role="CHALLENGER", training_start_ns=min(r["observed_ms"] for r in used)*1_000_000,
        training_end_ns=max(r["label_received_ms"] for r in used)*1_000_000,
        training_contracts=len({r["market_id"] for r in used}),
        training_days=len({r["observed_ms"]//86400000 for r in used}),
        assets=("BTC",), contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=tuple(sorted({r["rules_hash"] for r in used})), parameters=best["parameters"],
        hyperparameters={"automatic_promotion": False, "source_prefixes": source_manifest,
            "dataset_sha256": dataset_hash, "forward_oos_starts_after_ns": boundary_ns,
            "training_market_ids": sorted({r["market_id"] for r in used}),
            "development_market_ids": sorted({r["market_id"] for r in rows}),
            "split": "whole_market_60_20_20_label_availability_embargo",
            "selection": "validation_brier_then_logloss", "shuffle": False,
            "source_history_fix": "pre_v2_returns_excluded_not_zero_imputed",
            "paper_probe_only": True},
        oos_scores={"validation": best["validation"], "retrospective_audit": audit,
                    "audit_not_used_for_selection": True, "prospective_evidence": "NOT_YET_COLLECTED"},
        probability_interval_diagnostics={"validated": False, "bounds": [0.0, 1.0]},
        economic_replay={"execution_authority": "BOUNDED_PAPER_PROBE_ONLY", "promotion_credit": False,
                         "profitability_demonstrated": False}, generated_timestamp_ns=generated_ns)
    validate_parameters(artifact)
    report = {"schema": SCHEMA, "model_hash": artifact.model_hash, "code_sha": code_sha,
        "rows": len(rows), "contracts": len({r["market_id"] for r in rows}),
        "split_scores": {name: score(v, best["parameters"]) for name,v in parts.items()},
        "market_baseline_scores": {name: score(v, None) for name,v in parts.items()},
        "candidate_validation": [{"offset": c["parameters"]["offset"], "ridge": c["parameters"]["ridge"],
                                   **c["validation"]} for c in candidates],
        "selected_offset": best["parameters"]["offset"], "selected_ridge": best["parameters"]["ridge"],
        "active_features": best["parameters"]["feature_names"], "excluded_features": best["parameters"]["excluded_features"],
        "forward_start_ns": boundary_ns, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_promotion": False, "promotion_eligible": False,
        "state": "FROZEN_PAPER_RESEARCH_MODEL", "source_prefixes": source_manifest}
    return artifact, report


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape", action="append", type=Path, required=True)
    ap.add_argument("--registry", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--status", type=Path, required=True)
    args=ap.parse_args(); policy=json.loads(args.config.read_text()); registry=FairValueRegistry(args.registry)
    try:
        if not (policy.get("paper_only") is True and policy.get("authenticated_execution") is False
                and policy.get("real_order_submission") is False
                and (policy.get("paper_ml_probe") or {}).get("enabled") is True):
            raise ValueError("rich_train:paper_authority_not_configured")
        if registry.challenger_pointer.exists():
            pointer=json.loads(registry.challenger_pointer.read_text())
            artifact=FairModelArtifact(**json.loads(Path(pointer["artifact"]).read_text()))
            validate_parameters(artifact)
            if artifact.code_sha != args.model_sha or artifact.policy_version != canonical_hash(policy):
                raise ValueError("rich_train:existing_pointer_scope_mismatch")
            atomic_json(args.status,{"schema":SCHEMA,"state":"REUSED_IMMUTABLE_MODEL",
                "model_hash":artifact.model_hash,"paper_only":True,"automatic_promotion":False})
            return 0
        manifest=[]
        raw=records(args.tape,source_manifest=manifest,keep_types={"FORECAST","FORECAST_FINAL"})
        rows, exclusions=build_rows(list(raw.values()),time.time_ns()//1_000_000)
        if len({r["market_id"] for r in rows}) < 80:
            raise ValueError("rich_train:minimum_80_verified_markets")
        artifact, report=train(rows,args.model_sha,canonical_hash(policy),manifest)
        pointer=registry.publish_challenger(artifact)
        report.update(exclusions=exclusions,pointer=str(pointer))
        atomic_json(args.status,report)
        print(json.dumps({k:report[k] for k in ("state","model_hash","contracts","selected_offset","selected_ridge","active_features")},sort_keys=True))
        return 0
    except (ValueError,KeyError,OSError,TypeError) as exc:
        atomic_json(args.status,{"schema":SCHEMA,"state":"FAIL_CLOSED_RESEARCH_UNAVAILABLE","reason":str(exc),
                               "paper_only":True,"authenticated_execution":False,"real_order_submission":False})
        print(str(exc));return 2


if __name__ == "__main__":
    raise SystemExit(main())
