#!/usr/bin/env python3
"""Freeze one explicit multi-crypto PAPER-forward protocol.

This command does not tune parameters, inspect forward outcomes, or grant entry
authority. Every economic/statistical choice must already be present in the
input draft and all required evidence flags must be true before a protocol hash
is emitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from v7_lead_lag_replay import ASSETS, HORIZONS, ReplayError, digest

SHA40 = re.compile(r"^[0-9a-f]{40}$")
HASH64 = re.compile(r"^[0-9a-f]{64}$")
DRAFT_SCHEMA = "polymarket_v7_multi_crypto_forward_protocol_draft_v1"
FROZEN_SCHEMA = "polymarket_v7_multi_crypto_forward_protocol_v1"


def _number(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ReplayError(name)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayError(name) from exc
    if not math.isfinite(result):
        raise ReplayError(name)
    if positive and result <= 0:
        raise ReplayError(name)
    if nonnegative and result < 0:
        raise ReplayError(name)
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ReplayError(name)
    return value


def _hash(value: Any, name: str) -> str:
    text = str(value or "")
    if not HASH64.fullmatch(text):
        raise ReplayError(name)
    return text


def validate_draft(raw: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "paper_only", "authenticated_execution", "real_order_submission",
        "real_capital_at_risk", "automatic_promotion", "research_only",
        "experiment_id", "parent_experiment_id", "code_sha", "asset", "horizon",
        "data_cutoff_ns", "train_end_ns", "validation_end_ns", "embargo_ns",
        "feature_schema_hash", "model_hash", "fill_model_hash", "cost_model_hash",
        "settlement_semantic_hash", "latency_profile_id", "evidence",
        "entry_policy", "risk_policy", "statistical_protocol",
    }
    if not isinstance(raw, Mapping) or set(raw) != required or raw.get("schema") != DRAFT_SCHEMA:
        raise ReplayError("FORWARD_FREEZE_DRAFT_SHAPE_INVALID")
    if not (raw.get("paper_only") is True and raw.get("authenticated_execution") is False
            and raw.get("real_order_submission") is False and raw.get("real_capital_at_risk") is False
            and raw.get("automatic_promotion") is False and raw.get("research_only") is True):
        raise ReplayError("FORWARD_FREEZE_SAFETY_INVALID")
    experiment_id = str(raw.get("experiment_id") or "")
    if not experiment_id:
        raise ReplayError("FORWARD_FREEZE_EXPERIMENT_ID_MISSING")
    parent = raw.get("parent_experiment_id")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise ReplayError("FORWARD_FREEZE_PARENT_INVALID")
    code_sha = str(raw.get("code_sha") or "")
    if not SHA40.fullmatch(code_sha):
        raise ReplayError("FORWARD_FREEZE_EXACT_SHA_REQUIRED")
    asset, horizon = str(raw.get("asset") or ""), str(raw.get("horizon") or "")
    if asset not in ASSETS or horizon not in {"M5", "M15"}:
        raise ReplayError("FORWARD_FREEZE_SCOPE_INVALID")
    data_cutoff = _integer(raw.get("data_cutoff_ns"), "FORWARD_FREEZE_DATA_CUTOFF_INVALID", minimum=1)
    train_end = _integer(raw.get("train_end_ns"), "FORWARD_FREEZE_TRAIN_END_INVALID", minimum=1)
    validation_end = _integer(raw.get("validation_end_ns"), "FORWARD_FREEZE_VALIDATION_END_INVALID", minimum=1)
    embargo = _integer(raw.get("embargo_ns"), "FORWARD_FREEZE_EMBARGO_INVALID", minimum=0)
    if not train_end < validation_end <= data_cutoff:
        raise ReplayError("FORWARD_FREEZE_SPLIT_ORDER_INVALID")
    hashes = {name: _hash(raw.get(name), "FORWARD_FREEZE_HASH_INVALID:" + name) for name in (
        "feature_schema_hash", "model_hash", "fill_model_hash", "cost_model_hash",
        "settlement_semantic_hash",
    )}
    latency = str(raw.get("latency_profile_id") or "")
    if not latency:
        raise ReplayError("FORWARD_FREEZE_LATENCY_PROFILE_MISSING")

    evidence = raw.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "rules_verified", "token_mapping_verified", "oracle_binding_verified",
        "required_feeds_verified", "pm_book_replay_verified", "fee_model_verified",
        "fill_model_verified", "latency_profile_verified", "accounting_reconciled",
        "single_writer_verified", "training_artifact_frozen",
    } or any(value is not True for value in evidence.values()):
        raise ReplayError("FORWARD_FREEZE_EVIDENCE_INCOMPLETE")

    entry = raw.get("entry_policy")
    if not isinstance(entry, dict) or set(entry) != {
        "signal_definition_hash", "shock_threshold_z", "confirmation_rule",
        "minimum_tte_seconds", "maximum_tte_seconds", "maximum_signal_age_ms",
        "target_shares", "one_entry_per_market", "hold_to_settlement",
        "order_type", "price_rule", "require_full_visible_depth",
        "entry_uses_absolute_fair", "probability_source",
    }:
        raise ReplayError("FORWARD_FREEZE_ENTRY_POLICY_INVALID")
    _hash(entry.get("signal_definition_hash"), "FORWARD_FREEZE_SIGNAL_HASH_INVALID")
    _number(entry.get("shock_threshold_z"), "FORWARD_FREEZE_SHOCK_INVALID", positive=True)
    tte_min = _number(entry.get("minimum_tte_seconds"), "FORWARD_FREEZE_TTE_INVALID", positive=True)
    tte_max = _number(entry.get("maximum_tte_seconds"), "FORWARD_FREEZE_TTE_INVALID", positive=True)
    if tte_min >= tte_max:
        raise ReplayError("FORWARD_FREEZE_TTE_ORDER_INVALID")
    _integer(entry.get("maximum_signal_age_ms"), "FORWARD_FREEZE_SIGNAL_AGE_INVALID", minimum=1)
    _number(entry.get("target_shares"), "FORWARD_FREEZE_SIZE_INVALID", positive=True)
    if (not isinstance(entry.get("confirmation_rule"), str) or not entry["confirmation_rule"]
            or entry.get("one_entry_per_market") is not True
            or entry.get("hold_to_settlement") is not True
            or entry.get("order_type") != "FAK"
            or entry.get("price_rule") != "ARRIVAL_BEST_ASK_NO_CHASE"
            or entry.get("require_full_visible_depth") is not True
            or entry.get("entry_uses_absolute_fair") is not False
            or entry.get("probability_source") != "POLYMARKET_PRIOR_ONLY"):
        raise ReplayError("FORWARD_FREEZE_ENTRY_SEMANTICS_INVALID")

    risk = raw.get("risk_policy")
    if not isinstance(risk, dict) or set(risk) != {
        "cohort_maximum_loss_usd", "market_loss_cap_usd", "asset_loss_cap_usd",
        "horizon_loss_cap_usd", "parent_shock_loss_cap_usd", "portfolio_loss_cap_usd",
        "global_cash_checkpoint_required",
    }:
        raise ReplayError("FORWARD_FREEZE_RISK_POLICY_INVALID")
    for name in (
        "cohort_maximum_loss_usd", "market_loss_cap_usd", "asset_loss_cap_usd",
        "horizon_loss_cap_usd", "parent_shock_loss_cap_usd", "portfolio_loss_cap_usd",
    ):
        _number(risk.get(name), "FORWARD_FREEZE_RISK_INVALID:" + name, positive=True)
    if risk.get("global_cash_checkpoint_required") is not True:
        raise ReplayError("FORWARD_FREEZE_GLOBAL_CASH_REQUIRED")

    stats = raw.get("statistical_protocol")
    if not isinstance(stats, dict) or set(stats) != {
        "primary_endpoint", "target_independent_markets", "minimum_clusters",
        "bootstrap_block_ns", "bootstrap_seed", "bootstrap_draws",
        "forward_duration_seconds", "stopping_rule", "no_runtime_tuning",
        "common_time_split_across_assets",
    }:
        raise ReplayError("FORWARD_FREEZE_STATISTICS_INVALID")
    if not isinstance(stats.get("primary_endpoint"), str) or not stats["primary_endpoint"]:
        raise ReplayError("FORWARD_FREEZE_ENDPOINT_MISSING")
    _integer(stats.get("target_independent_markets"), "FORWARD_FREEZE_TARGET_INVALID", minimum=1)
    _integer(stats.get("minimum_clusters"), "FORWARD_FREEZE_CLUSTERS_INVALID", minimum=2)
    _integer(stats.get("bootstrap_block_ns"), "FORWARD_FREEZE_BLOCK_INVALID", minimum=1)
    if type(stats.get("bootstrap_seed")) is not int:
        raise ReplayError("FORWARD_FREEZE_SEED_INVALID")
    _integer(stats.get("bootstrap_draws"), "FORWARD_FREEZE_DRAWS_INVALID", minimum=100)
    _integer(stats.get("forward_duration_seconds"), "FORWARD_FREEZE_DURATION_INVALID", minimum=1)
    if (not isinstance(stats.get("stopping_rule"), str) or not stats["stopping_rule"]
            or stats.get("no_runtime_tuning") is not True
            or stats.get("common_time_split_across_assets") is not True):
        raise ReplayError("FORWARD_FREEZE_STATISTICS_SEMANTICS_INVALID")
    return json.loads(json.dumps(raw, sort_keys=True))


def freeze(raw: Mapping[str, Any]) -> dict[str, Any]:
    draft = validate_draft(raw)
    normalized = dict(draft)
    normalized["schema"] = FROZEN_SCHEMA
    protocol_hash = digest(normalized)
    packet = {
        "mode": "PAPER_MULTI_CRYPTO_FORWARD",
        "experiment_id": draft["experiment_id"],
        "protocol_hash": protocol_hash,
        "feature_schema_hash": draft["feature_schema_hash"],
        "model_hash": draft["model_hash"],
        "fill_model_hash": draft["fill_model_hash"],
        "cost_model_hash": draft["cost_model_hash"],
        "settlement_semantic_hash": draft["settlement_semantic_hash"],
        "latency_profile_id": draft["latency_profile_id"],
        "asset": draft["asset"], "horizon": draft["horizon"],
        "research_only": True, "automatic_promotion": False,
        "one_entry_per_market": True, "hold_to_settlement": True,
        "entry_uses_absolute_fair": False, "probability_source": "POLYMARKET_PRIOR_ONLY",
    }
    return {
        "schema": FROZEN_SCHEMA, "protocol_hash": protocol_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "automatic_promotion": False, "entry_authority": False,
        "frozen_protocol": normalized, "multi_crypto_forward": packet,
        "note": "Immutable research protocol. It cannot enable runtime execution by itself.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or "ledger" in args.output.parts:
        parser.exit(2, "refusing overwrite or canonical ledger destination\n")
    try:
        report = freeze(json.loads(args.input.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False); handle.write("\n")
        print(json.dumps({"protocol_hash": report["protocol_hash"], "entry_authority": False,
                          "automatic_promotion": False}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"forward freeze failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
