#!/usr/bin/env python3
"""Freeze a PM-as-offset external residual model for prospective PAPER research.

The Polymarket probability is a fixed logit offset. External information may
only learn a residual correction; shrinkage zero is exactly the PM baseline.
This module has no execution, cancel, capital, inventory, or ledger authority.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from v7_external_economic_common import atomic_json
from v7_external_rich_model import FAMILY, FEATURE_SCHEMA, validate_parameters
from v7_external_rich_train import build_rows, fit, records, score, split_rows
from v7_fair_model_artifact import FairModelArtifact, canonical_hash

SCHEMA = "polymarket_v7_residual_overlay_training_v1"


def train_residual(
    rows: list[dict[str, Any]], code_sha: str, policy: dict[str, Any],
    source_manifest: list[dict[str, Any]], generated_ns: int | None = None,
) -> tuple[FairModelArtifact, dict[str, Any]]:
    parts = split_rows(rows)
    if any(len({r["market_id"] for r in values}) < 8 for values in parts.values()):
        raise ValueError("residual_train:insufficient_temporal_partitions")
    model_cfg = policy.get("model") if isinstance(policy.get("model"), dict) else {}
    if model_cfg.get("allowed_offset") != "market":
        raise ValueError("residual_train:market_offset_required")
    ridges = [float(x) for x in model_cfg.get("ridge_grid", [])]
    shrinkages = [float(x) for x in model_cfg.get("correction_shrinkage_grid", [])]
    if not ridges or not shrinkages or 0.0 not in shrinkages:
        raise ValueError("residual_train:frozen_grid_invalid")

    candidates: list[dict[str, Any]] = []
    for ridge in ridges:
        base = fit(parts["train"], ridge, "market")
        for shrinkage in shrinkages:
            if not 0.0 <= shrinkage <= 1.0:
                raise ValueError("residual_train:shrinkage_out_of_range")
            parameters = dict(
                base,
                coefficients=[shrinkage * value for value in base["coefficients"]],
                correction_shrinkage=shrinkage,
                residual_only=True,
                baseline_semantics="FIXED_POLYMARKET_LOGIT_OFFSET",
            )
            candidates.append({
                "parameters": parameters,
                "validation": score(parts["validation"], parameters),
            })
    best = min(candidates, key=lambda c: (
        c["validation"]["brier"], c["validation"]["log_loss"],
        c["parameters"]["correction_shrinkage"], -c["parameters"]["ridge"],
    ))
    generated_ns = time.time_ns() if generated_ns is None else generated_ns
    if max(r["label_received_ms"] for r in rows) * 1_000_000 >= generated_ns:
        raise ValueError("residual_train:future_training_label")
    boundary_ns = ((generated_ns // 1_000_000_000 // 300) + 1) * 300 * 1_000_000_000
    used = parts["train"] + parts["validation"]
    dataset_hash = canonical_hash({"rows": rows})
    policy_hash = canonical_hash(policy)
    artifact = FairModelArtifact.build(
        family=FAMILY,
        model_version="btc-m5-pm-residual-" + dataset_hash[:16],
        feature_schema_version=FEATURE_SCHEMA,
        code_sha=code_sha,
        policy_version=policy_hash,
        artifact_role="RESEARCH",
        training_start_ns=min(r["observed_ms"] for r in used) * 1_000_000,
        training_end_ns=max(r["label_received_ms"] for r in used) * 1_000_000,
        training_contracts=len({r["market_id"] for r in used}),
        training_days=len({r["observed_ms"] // 86_400_000 for r in used}),
        assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=tuple(sorted({r["rules_hash"] for r in used})),
        parameters=best["parameters"],
        hyperparameters={
            "research_only": True,
            "pm_as_fixed_offset": True,
            "residual_only": True,
            "source_prefixes": source_manifest,
            "dataset_sha256": dataset_hash,
            "forward_oos_starts_after_ns": boundary_ns,
            "same_artifact_required_across_replications": True,
            "replication_count": int((policy.get("prospective") or {}).get("replication_count", 0)),
            "duration_seconds_each": int((policy.get("prospective") or {}).get("duration_seconds_each", 0)),
            "training_market_ids": sorted({r["market_id"] for r in used}),
            "development_market_ids": sorted({r["market_id"] for r in rows}),
            "split": "whole_market_60_20_20_label_availability_embargo",
            "selection": "validation_brier_then_logloss_residual_only",
            "shuffle": False,
        },
        oos_scores={
            "validation": best["validation"],
            "retrospective_audit": score(parts["audit"], best["parameters"]),
            "market_baseline_validation": score(parts["validation"], None),
            "audit_not_used_for_selection": True,
            "prospective_evidence": "NOT_YET_COLLECTED",
        },
        probability_interval_diagnostics={"validated": False, "bounds": [0.0, 1.0]},
        economic_replay={
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "research_only": True,
            "profitability_demonstrated": False,
        },
        generated_timestamp_ns=generated_ns,
    )
    validate_parameters(artifact)
    report = {
        "schema": SCHEMA,
        "state": "FROZEN_RESIDUAL_RESEARCH_MODEL",
        "model_hash": artifact.model_hash,
        "selected_offset": best["parameters"]["offset"],
        "selected_ridge": best["parameters"]["ridge"],
        "selected_correction_shrinkage": best["parameters"]["correction_shrinkage"],
        "validation": best["validation"],
        "market_baseline_validation": score(parts["validation"], None),
        "retrospective_audit": score(parts["audit"], best["parameters"]),
        "forward_start_ns": boundary_ns,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
    }
    return artifact, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape", action="append", type=Path, required=True)
    ap.add_argument("--protocol", type=Path, required=True)
    ap.add_argument("--output-model", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--replace-research-model", action="store_true")
    args = ap.parse_args()
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    try:
        if (
            protocol.get("schema") != "polymarket_v7_residual_overlay_protocol_v1"
            or protocol.get("paper_only") is not True
            or protocol.get("authenticated_execution") is not False
            or protocol.get("real_order_submission") is not False
            or protocol.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
            or protocol.get("automatic_promotion") is not False
            or len(args.model_sha) != 40
        ):
            raise ValueError("residual_train:safety_contract")
        if args.output_model.exists() and not args.replace_research_model:
            artifact = FairModelArtifact(**json.loads(args.output_model.read_text()))
            validate_parameters(artifact)
            if artifact.parameters.get("offset") != "market" or artifact.parameters.get("residual_only") is not True:
                raise ValueError("residual_train:existing_artifact_not_residual")
            atomic_json(args.status, {
                "schema": SCHEMA, "state": "REUSED_FROZEN_RESIDUAL_MODEL",
                "model_hash": artifact.model_hash, "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            })
            return 0
        manifest: list[dict[str, Any]] = []
        raw = records(args.tape, source_manifest=manifest, keep_types={"FORECAST", "FORECAST_FINAL"})
        rows, exclusions = build_rows(list(raw.values()), time.time_ns() // 1_000_000)
        if len({r["market_id"] for r in rows}) < 80:
            raise ValueError("residual_train:minimum_80_verified_markets")
        artifact, report = train_residual(rows, args.model_sha, protocol, manifest)
        args.output_model.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output_model, artifact.__dict__)
        report.update(exclusions=exclusions, model_path=str(args.output_model))
        atomic_json(args.status, report)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        atomic_json(args.status, {
            "schema": SCHEMA, "state": "FAIL_CLOSED_RESEARCH_UNAVAILABLE",
            "reason": str(exc), "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        })
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
