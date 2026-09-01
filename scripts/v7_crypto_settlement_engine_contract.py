#!/usr/bin/env python3
"""Freeze fail-closed crypto settlement-engine inputs for the bounded C++ kernel.

This slow-plane tool grants no execution authority.  It converts independently
versioned horizon, latency and maker-execution evidence into one immutable
runtime snapshot.  Missing or mismatched evidence leaves new risk disabled;
critical cancel/withdraw paths do not depend on this snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from v7_crypto_settlement import (
    CryptoAsset, CryptoHorizon, CryptoSettlementError, require_context,
    validate_model_artifact, validate_model_registry,
    validate_registry as validate_market_registry,
)


CONFIG_SCHEMA = "polymarket_v7_crypto_settlement_engine_v1"
STRUCTURAL_CONFIG_SCHEMA = "polymarket_v7_structural_arb_engine_v1"
SNAPSHOT_SCHEMA = "polymarket_v7_crypto_settlement_runtime_snapshot_v1"
LATENCY_SCHEMA = "polymarket_v7_empirical_latency_profile_v1"
MAKER_SCHEMA = "polymarket_v7_maker_execution_evidence_v1"
SHA40 = re.compile(r"[0-9a-f]{40}")
ALLOWED_HORIZONS = {300, 900}
REQUIRED_LATENCY_SEGMENTS = (
    "taker_arrival", "maker_place_ack", "maker_cancel_ack",
    "private_ws_confirmation",
)
REQUIRED_PERCENTILE_FIELDS = (
    "p50_ms", "p90_ms", "p95_ms", "p99_ms", "p99_9_ms", "max_ms",
)


class ContractError(ValueError):
    pass


def _json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return output if math.isfinite(output) else None


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def validate_config(config: dict[str, Any]) -> None:
    if (
        config.get("schema") != CONFIG_SCHEMA
        or config.get("paper_only") is not True
        or config.get("authenticated_execution") is not False
        or config.get("real_order_submission") is not False
        or config.get("automatic_promotion") is not False
        or config.get("decision_owner") != "CRYPTO_SETTLEMENT_ENGINE"
        or config.get("market_registry") != "config/v7_crypto_settlement_markets.json"
        or config.get("model_registry") != "config/v7_crypto_settlement_model_registry.json"
        or config.get("authority_registry") != "config/v7_authority_registry.json"
        or config.get("opportunity_contract") != "schemas/v7/opportunity_envelope.schema.json"
        or config.get("global_portfolio_coordinator") != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        or config.get("component_independent_authority") is not False
    ):
        raise ContractError("engine_identity_or_safety")
    if set(config.get("action_space") or []) != {"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"}:
        raise ContractError("unified_action_space")
    components = set(config.get("component_families") or [])
    if components != {
        "crypto_settlement_fair", "crypto_informed_taker", "professional_maker",
    }:
        raise ContractError("crypto_component_partition")
    fair = config.get("fair_value") if isinstance(config.get("fair_value"), dict) else {}
    if (
        fair.get("separate_model_per_horizon") is not True
        or fair.get("separate_frozen_model_per_asset_horizon") is not True
        or fair.get("model_registry_index") != [
            "asset", "horizon", "settlement_semantic_hash",
        ]
        or fair.get("fixed_bridge_coefficient_authorized") is not False
        or fair.get("empirical_frozen_artifact_required") is not True
        or fair.get("settlement_source") != "REGISTRY_VERIFIED_CHAINLINK_TWAP_60S"
        or fair.get("settlement_source_may_be_replaced_by_predictor") is not False
    ):
        raise ContractError("fair_value_binding")
    external_updates = config.get("external_update_policy") if isinstance(
        config.get("external_update_policy"), dict
    ) else {}
    if (
        external_updates.get("oracle_or_external_update_can_cancel_without_polymarket_book_event") is not True
        or external_updates.get("oracle_or_external_update_can_reprice_without_polymarket_book_event") is not True
        or external_updates.get("stale_quote_behavior") != "CANCEL_OR_NOTHING"
    ):
        raise ContractError("external_update_risk_preemption")
    maker_boundary = config.get("maker_economics") if isinstance(
        config.get("maker_economics"), dict
    ) else {}
    required_maker_components = {
        "quote_candidate_generation", "reach_estimation",
        "conditional_fill_given_reach_estimation", "queue_priority_model",
        "fill_conditioned_markout", "cancel_latency_stale_risk_model",
        "inventory_aware_skew", "fee_and_rebate_authority_required",
        "quote_lifecycle_diagnostics",
    }
    if (
        any(maker_boundary.get(name) is not True for name in required_maker_components)
        or maker_boundary.get("independent_capital_oms_inventory_ledger_authority") is not False
        or config.get("risk_actions_preempt_alpha") is not True
    ):
        raise ContractError("maker_component_boundary")
    rows = config.get("horizons")
    if not isinstance(rows, list) or len(rows) != 2:
        raise ContractError("horizon_count")
    found: set[int] = set()
    scopes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("horizon_row")
        duration = int(row.get("horizon_seconds") or 0)
        scope = str(row.get("model_scope") or "")
        if duration not in ALLOWED_HORIZONS or duration in found or not scope or scope in scopes:
            raise ContractError("horizon_identity")
        found.add(duration)
        scopes.add(scope)
        research = row.get("research_only") is True
        for name in ("maker", "taker"):
            policy = row.get(name) if isinstance(row.get(name), dict) else {}
            minimum = int(policy.get("minimum_tte_seconds") or 0)
            maximum = int(policy.get("maximum_tte_seconds") or 0)
            enabled = policy.get("enabled") is True
            if minimum < 0 or maximum < minimum or maximum > duration:
                raise ContractError(f"{name}_tte_window")
            if research and enabled:
                raise ContractError("research_horizon_has_execution")
    if found != ALLOWED_HORIZONS:
        raise ContractError("horizon_partition")
    research = set(config.get("research_zero_authority_families") or [])
    if len(research) != 10 or research & components:
        raise ContractError("research_zero_authority_partition")


def validate_structural_config(config: dict[str, Any]) -> None:
    components = set(config.get("component_families") or [])
    true_fields = {
        "full_depth_required", "direct_joint_completion_required",
        "one_atomic_economic_intent_per_bundle", "one_capital_reservation_per_bundle",
        "partial_fill_plan_required", "timeout_plan_required",
        "full_depth_bounded_unwind_required",
    }
    if (
        config.get("schema") != STRUCTURAL_CONFIG_SCHEMA
        or config.get("version") != 1
        or config.get("paper_only") is not True
        or config.get("authenticated_execution") is not False
        or config.get("real_order_submission") is not False
        or config.get("real_capital_at_risk") is not False
        or config.get("automatic_promotion") is not False
        or config.get("decision_owner") != "STRUCTURAL_ARB_ENGINE"
        or config.get("authority_registry") != "config/v7_authority_registry.json"
        or config.get("opportunity_contract") != "schemas/v7/opportunity_envelope.schema.json"
        or config.get("global_portfolio_coordinator") != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        or components != {"hard_arb", "fast_structural"}
        or config.get("component_independent_authority") is not False
        or set(config.get("action_space") or []) != {"ARB", "CANCEL", "NOTHING"}
        or any(config.get(name) is not True for name in true_fields)
        or config.get("near_miss_evidence_has_execution_authority") is not False
        or config.get("new_risk_default") != "CANCEL_AND_NOTHING_ONLY"
    ):
        raise ContractError("structural_engine_contract")
    owners = {
        "capital_envelope_owner": "V7_CANONICAL_ALLOCATOR",
        "risk_owner": "V7_CANONICAL_RISK", "oms_owner": "V7_CANONICAL_OMS",
        "inventory_owner": "V7_CANONICAL_INVENTORY", "ledger_owner": "V7_CANONICAL_LEDGER",
    }
    if any(config.get(key) != owner for key, owner in owners.items()):
        raise ContractError("structural_shared_owner_contract")


def validate_registry_authority(
    config: dict[str, Any], structural: dict[str, Any], registry: dict[str, Any],
) -> None:
    if registry.get("schema") != "polymarket_v7_strategy_registry_v1":
        raise ContractError("strategy_registry_schema")
    rows = registry.get("strategies")
    if not isinstance(rows, list):
        raise ContractError("strategy_registry_rows")
    authorities = {
        str(row.get("family") or ""): str(row.get("authority") or "").upper()
        for row in rows if isinstance(row, dict) and row.get("enabled") is True
    }
    if len(authorities) != 15:
        raise ContractError("strategy_registry_partition")
    engines = registry.get("economic_engines") if isinstance(
        registry.get("economic_engines"), dict) else {}
    expected_owners = {"CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"}
    for name in expected_owners:
        engine = engines.get(name) if isinstance(engines.get(name), dict) else {}
        if (
            engine.get("authority_owner") != name
            or engine.get("component_independent_authority") is not False
        ):
            raise ContractError(f"economic_engine_authority:{name}")
    if set(engines["CRYPTO_SETTLEMENT_ENGINE"].get("components") or []) != {
        "crypto_settlement_fair", "professional_maker", "crypto_informed_taker",
    }:
        raise ContractError("crypto_engine_registry_components")
    if set(engines["STRUCTURAL_ARB_ENGINE"].get("components") or []) != {
        "hard_arb", "fast_structural",
    }:
        raise ContractError("structural_engine_registry_components")
    independent_paper = {
        family for family, authority in authorities.items() if authority == "PAPER"
    }
    if independent_paper:
        raise ContractError("component_has_independent_paper_authority")
    component_families = set(config["component_families"])
    component_families.update(structural["component_families"])
    if any(authorities.get(family) not in {"SHADOW", "RESEARCH"}
           for family in component_families):
        raise ContractError("component_has_independent_authority")
    if any(authorities.get(family) != "RESEARCH"
           for family in config["research_zero_authority_families"]):
        raise ContractError("research_family_has_authority")


def validate_live_scope(
    config: dict[str, Any], structural: dict[str, Any], scope: dict[str, Any],
) -> None:
    if (
        scope.get("schema") != "polymarket_v7_live_model_scope_v1"
        or scope.get("paper_only") is not True
        or scope.get("authenticated_execution") is not False
        or scope.get("real_order_submission") is not False
    ):
        raise ContractError("live_scope_identity_or_safety")
    if set(scope.get("paper_execution_engines") or []) != {
        "CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE",
    }:
        raise ContractError("live_scope_must_have_two_economic_owners")
    if set(scope.get("component_shadow_families") or []) != {
        "crypto_settlement_fair", "professional_maker", "crypto_informed_taker",
        "hard_arb", "fast_structural",
    }:
        raise ContractError("live_scope_component_shadows")
    if set(scope.get("research_zero_authority_families") or []) != set(
        config["research_zero_authority_families"]
    ):
        raise ContractError("live_scope_research_zero_authority")
    if scope.get("crypto_settlement_engine_contract") != \
            "config/v7_crypto_settlement_engine.json":
        raise ContractError("live_scope_engine_contract_path")
    if (
        scope.get("crypto_settlement_market_registry") !=
        "config/v7_crypto_settlement_markets.json"
        or scope.get("crypto_settlement_model_registry") !=
        "config/v7_crypto_settlement_model_registry.json"
    ):
        raise ContractError("live_scope_crypto_registry_paths")
    if scope.get("structural_arb_engine_contract") != \
            "config/v7_structural_arb_engine.json":
        raise ContractError("live_scope_structural_contract_path")


def _horizon(config: dict[str, Any], horizon_seconds: int) -> dict[str, Any]:
    return next(
        row for row in config["horizons"]
        if int(row["horizon_seconds"]) == horizon_seconds
    )


def latency_snapshot(
    raw: dict[str, Any], *, code_sha: str, minimum_samples: int,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    if raw.get("schema") != LATENCY_SCHEMA:
        blockers.append("EMPIRICAL_LATENCY_PROFILE_MISSING")
    if int(raw.get("profile_version") or 0) <= 0:
        blockers.append("LATENCY_PROFILE_VERSION_INVALID")
    if raw.get("exact_code_sha") != code_sha:
        blockers.append("LATENCY_EXACT_SHA_MISMATCH")
    if (
        raw.get("paper_only") is not True
        or raw.get("authenticated_execution") is not False
        or raw.get("real_order_submission") is not False
    ):
        blockers.append("LATENCY_SAFETY_CONTRACT_INVALID")
    if raw.get("configured_constants_used") is not False:
        blockers.append("LATENCY_PROFILE_USES_CONFIGURED_CONSTANTS")
    segments = raw.get("segments") if isinstance(raw.get("segments"), dict) else {}
    p99: dict[str, float] = {}
    counts: list[int] = []
    for name in REQUIRED_LATENCY_SEGMENTS:
        row = segments.get(name) if isinstance(segments.get(name), dict) else {}
        percentiles = [_finite(row.get(field)) for field in REQUIRED_PERCENTILE_FIELDS]
        value = percentiles[3]
        count = int(row.get("samples") or 0)
        distribution_valid = (
            all(item is not None and item >= 0.0 for item in percentiles)
            and all(
                float(left) <= float(right)
                for left, right in zip(percentiles, percentiles[1:])
            )
        )
        if not distribution_valid or value is None or count < minimum_samples:
            blockers.append(f"LATENCY_SEGMENT_INSUFFICIENT:{name}")
        else:
            p99[name] = value / 1_000.0
            counts.append(count)
    valid = not blockers
    return {
        "profile_version": int(raw.get("profile_version") or 0) if valid else 0,
        "sample_count": min(counts) if valid and counts else 0,
        "taker_arrival_p99_seconds": p99.get("taker_arrival", 0.0),
        "maker_place_p99_seconds": p99.get("maker_place_ack", 0.0),
        "maker_cancel_p99_seconds": p99.get("maker_cancel_ack", 0.0),
        "private_ws_confirmation_p99_seconds": p99.get("private_ws_confirmation", 0.0),
        "empirical": valid,
        "valid": valid,
    }, blockers


def maker_snapshot(
    raw: dict[str, Any], *, code_sha: str, minimum_orders: int,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    if raw.get("schema") != MAKER_SCHEMA:
        blockers.append("MAKER_EXECUTION_EVIDENCE_MISSING")
    if int(raw.get("model_version") or 0) <= 0:
        blockers.append("MAKER_EXECUTION_MODEL_VERSION_INVALID")
    if raw.get("exact_code_sha") != code_sha:
        blockers.append("MAKER_EXECUTION_EXACT_SHA_MISMATCH")
    if (
        raw.get("paper_only") is not True
        or raw.get("authenticated_execution") is not False
        or raw.get("real_order_submission") is not False
    ):
        blockers.append("MAKER_EXECUTION_SAFETY_CONTRACT_INVALID")
    orders = int(raw.get("independent_orders") or 0)
    reach = _finite(raw.get("reach_probability_lower"))
    conditional_fill = _finite(raw.get("fill_given_reach_probability_lower"))
    markout = _finite(raw.get("adverse_markout_upper_per_share"))
    if orders < minimum_orders:
        blockers.append("MAKER_EXECUTION_ORDERS_INSUFFICIENT")
    if reach is None or not 0.0 <= reach <= 1.0:
        blockers.append("MAKER_REACH_PROBABILITY_INVALID")
    if conditional_fill is None or not 0.0 <= conditional_fill <= 1.0:
        blockers.append("MAKER_CONDITIONAL_FILL_PROBABILITY_INVALID")
    if markout is None or markout < 0.0:
        blockers.append("MAKER_FILL_CONDITIONED_MARKOUT_INVALID")
    if raw.get("fill_conditioned_markout") is not True:
        blockers.append("MAKER_MARKOUT_NOT_FILL_CONDITIONED")
    mature = not blockers and raw.get("mature") is True
    if not mature and "MAKER_EXECUTION_EVIDENCE_IMMATURE" not in blockers:
        blockers.append("MAKER_EXECUTION_EVIDENCE_IMMATURE")
    return {
        "execution_model_version": int(raw.get("model_version") or 0) if mature else 0,
        "execution_model_mature": mature,
        "markout_model_mature": mature,
        "reach_probability_lower": reach or 0.0,
        "fill_given_reach_probability_lower": conditional_fill or 0.0,
        "adverse_markout_upper_per_share": markout or 0.0,
        "valid": mature,
    }, blockers


def freeze(
    config: dict[str, Any], *, code_sha: str, asset: str, horizon_name: str,
    structural_config: dict[str, Any], registry: dict[str, Any],
    live_scope: dict[str, Any], market_registry: dict[str, Any],
    model_registry: dict[str, Any],
    latency_profile: dict[str, Any] | None = None,
    maker_evidence: dict[str, Any] | None = None,
    model_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_config(config)
    validate_structural_config(structural_config)
    validate_registry_authority(config, structural_config, registry)
    validate_live_scope(config, structural_config, live_scope)
    if SHA40.fullmatch(code_sha) is None:
        raise ContractError("exact_code_sha")
    try:
        contexts = validate_market_registry(market_registry)
        context = require_context(contexts, asset, horizon_name)
        registered_models = validate_model_registry(model_registry, contexts)
    except CryptoSettlementError as exc:
        raise ContractError(str(exc)) from exc
    horizon_seconds = context.horizon_seconds
    horizon = _horizon(config, horizon_seconds)
    latency_cfg = config["latency"]
    maker_cfg = config["maker_economics"]
    latency, latency_blockers = latency_snapshot(
        latency_profile or {}, code_sha=code_sha,
        minimum_samples=int(latency_cfg["minimum_samples"]),
    )
    maker, maker_blockers = maker_snapshot(
        maker_evidence or {}, code_sha=code_sha,
        minimum_orders=int(maker_cfg["minimum_orders"]),
    )
    model_blockers: list[str] = []
    model_valid = False
    try:
        registered = registered_models[(context.asset, context.horizon)]
        if registered is None:
            raise CryptoSettlementError("model_unregistered")
        supplied = validate_model_artifact(model_artifact or registered, context)
        if _canonical_hash(supplied) != _canonical_hash(registered):
            raise CryptoSettlementError("model_not_canonical_registry_artifact")
        model_valid = True
    except CryptoSettlementError as exc:
        model_blockers.append(f"MODEL_INVALID:{exc}")
    research_only = context.research_only or horizon.get("research_only") is True
    maker_policy, taker_policy = horizon["maker"], horizon["taker"]
    maker_enabled = bool(
        not research_only and maker_policy.get("enabled") is True
        and latency["valid"] and maker["valid"] and model_valid
    )
    taker_enabled = bool(
        not research_only and taker_policy.get("enabled") is True
        and latency["valid"] and model_valid
    )
    blockers = sorted(set(latency_blockers + maker_blockers + model_blockers))
    runtime = {
        "schema": SNAPSHOT_SCHEMA,
        "exact_code_sha": code_sha,
        "config_sha256": _canonical_hash(config),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "decision_owner": "CRYPTO_SETTLEMENT_ENGINE",
        "crypto_context": {
            "asset": context.asset.value,
            "horizon": context.horizon.value,
            "context_id": context.context_id,
            "contract_family": context.contract_family,
            "settlement_semantic_hash": context.settlement_semantic_hash,
            "authority": context.authority,
        },
        "model_binding_valid": model_valid,
        "horizon_policy": {
            "policy_version": int(config["version"]),
            "horizon_seconds": horizon_seconds,
            "maker_min_tte_ns": int(maker_policy["minimum_tte_seconds"]) * 1_000_000_000,
            "maker_max_tte_ns": int(maker_policy["maximum_tte_seconds"]) * 1_000_000_000,
            "taker_min_tte_ns": int(taker_policy["minimum_tte_seconds"]) * 1_000_000_000,
            "taker_max_tte_ns": int(taker_policy["maximum_tte_seconds"]) * 1_000_000_000,
            "maker_enabled": maker_enabled,
            "taker_enabled": taker_enabled,
            "research_only": research_only,
            "valid": True,
        },
        "latency": latency,
        "maker_execution": maker,
        "new_risk_authorized": maker_enabled or taker_enabled,
        "cancel_path_independent": True,
        "blockers": blockers,
    }
    runtime["snapshot_sha256"] = _canonical_hash(runtime)
    return runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_crypto_settlement_engine.json"))
    parser.add_argument("--structural-config", type=Path, default=Path("config/v7_structural_arb_engine.json"))
    parser.add_argument("--registry", type=Path, default=Path("config/v7_strategy_registry.json"))
    parser.add_argument("--live-scope", type=Path, default=Path("config/v7_live_model_scope.json"))
    parser.add_argument("--market-registry", type=Path, default=Path("config/v7_crypto_settlement_markets.json"))
    parser.add_argument("--model-registry", type=Path, default=Path("config/v7_crypto_settlement_model_registry.json"))
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--asset", required=True, choices=[asset.value for asset in CryptoAsset])
    parser.add_argument("--horizon", required=True, choices=[horizon.value for horizon in CryptoHorizon])
    parser.add_argument("--latency-profile", type=Path)
    parser.add_argument("--maker-evidence", type=Path)
    parser.add_argument("--model-artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = freeze(
        _json(args.config), code_sha=args.code_sha,
        structural_config=_json(args.structural_config), asset=args.asset,
        horizon_name=args.horizon,
        registry=_json(args.registry),
        live_scope=_json(args.live_scope),
        market_registry=_json(args.market_registry),
        model_registry=_json(args.model_registry),
        latency_profile=_json(args.latency_profile),
        maker_evidence=_json(args.maker_evidence),
        model_artifact=_json(args.model_artifact),
    )
    _atomic_json(args.output, snapshot)
    print(json.dumps({
        "new_risk_authorized": snapshot["new_risk_authorized"],
        "blockers": snapshot["blockers"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
