#!/usr/bin/env python3
"""Freeze fail-closed BTC settlement-engine inputs for the bounded C++ kernel.

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


CONFIG_SCHEMA = "polymarket_v7_btc_settlement_engine_v1"
SNAPSHOT_SCHEMA = "polymarket_v7_btc_settlement_runtime_snapshot_v1"
LATENCY_SCHEMA = "polymarket_v7_empirical_latency_profile_v1"
MAKER_SCHEMA = "polymarket_v7_maker_execution_evidence_v1"
SHA40 = re.compile(r"[0-9a-f]{40}")
ALLOWED_HORIZONS = {300, 900, 14_400}
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
        or config.get("decision_owner") != "BTC_SETTLEMENT_ENGINE"
        or config.get("component_independent_authority") is not False
    ):
        raise ContractError("engine_identity_or_safety")
    if set(config.get("action_space") or []) != {"MAKE", "TAKE", "CANCEL", "NOTHING"}:
        raise ContractError("unified_action_space")
    components = set(config.get("component_families") or [])
    if components != {
        "crypto_settlement_fair", "crypto_informed_taker", "professional_maker",
    }:
        raise ContractError("btc_component_partition")
    fair = config.get("fair_value") if isinstance(config.get("fair_value"), dict) else {}
    if (
        fair.get("separate_model_per_horizon") is not True
        or fair.get("fixed_bridge_coefficient_authorized") is not False
        or fair.get("empirical_frozen_artifact_required") is not True
        or fair.get("settlement_source") != "CHAINLINK_60_SECOND_TWAP"
        or fair.get("settlement_source_may_be_replaced_by_predictor") is not False
    ):
        raise ContractError("fair_value_binding")
    rows = config.get("horizons")
    if not isinstance(rows, list) or len(rows) != 3:
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
    structural = config.get("structural_arb_engine") if isinstance(
        config.get("structural_arb_engine"), dict) else {}
    structural_components = set(structural.get("component_families") or [])
    if (
        structural.get("decision_owner") != "STRUCTURAL_ARB_ENGINE"
        or structural_components != {"hard_arb", "fast_structural"}
        or structural.get("component_independent_authority") is not False
        or structural.get("full_depth_required") is not True
        or structural.get("direct_joint_completion_required") is not True
        or structural.get("partial_unwind_required") is not True
    ):
        raise ContractError("structural_engine_contract")
    research = set(config.get("research_zero_authority_families") or [])
    if len(research) != 10 or research & (components | structural_components):
        raise ContractError("research_zero_authority_partition")
    if len(research | components | structural_components) != 15:
        raise ContractError("economic_engine_family_partition")


def validate_registry_authority(
    config: dict[str, Any], registry: dict[str, Any],
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
    expected_owners = {
        "BTC_SETTLEMENT_ENGINE": "crypto_settlement_fair",
        "STRUCTURAL_ARB_ENGINE": "hard_arb",
    }
    for name, owner in expected_owners.items():
        engine = engines.get(name) if isinstance(engines.get(name), dict) else {}
        if (
            engine.get("authority_owner") != owner
            or engine.get("component_independent_authority") is not False
            or authorities.get(owner) != "PAPER"
        ):
            raise ContractError(f"economic_engine_authority:{name}")
    if set(engines["BTC_SETTLEMENT_ENGINE"].get("components") or []) != {
        "professional_maker", "crypto_informed_taker",
    }:
        raise ContractError("btc_engine_registry_components")
    if set(engines["STRUCTURAL_ARB_ENGINE"].get("components") or []) != {
        "fast_structural",
    }:
        raise ContractError("structural_engine_registry_components")
    independent_paper = {
        family for family, authority in authorities.items() if authority == "PAPER"
    }
    if independent_paper != set(expected_owners.values()):
        raise ContractError("independent_paper_authority_not_two_engines")
    component_families = set(config["component_families"])
    component_families.remove("crypto_settlement_fair")
    component_families.update(
        config["structural_arb_engine"]["component_families"]
    )
    component_families.remove("hard_arb")
    if any(authorities.get(family) not in {"SHADOW", "RESEARCH"}
           for family in component_families):
        raise ContractError("component_has_independent_authority")
    if any(authorities.get(family) != "RESEARCH"
           for family in config["research_zero_authority_families"]):
        raise ContractError("research_family_has_authority")


def validate_live_scope(config: dict[str, Any], scope: dict[str, Any]) -> None:
    if (
        scope.get("schema") != "polymarket_v7_live_model_scope_v1"
        or scope.get("paper_only") is not True
        or scope.get("authenticated_execution") is not False
        or scope.get("real_order_submission") is not False
    ):
        raise ContractError("live_scope_identity_or_safety")
    if set(scope.get("paper_execution_families") or []) != {
        "crypto_settlement_fair", "hard_arb",
    }:
        raise ContractError("live_scope_must_have_two_economic_owners")
    if set(scope.get("component_shadow_families") or []) != {
        "professional_maker", "crypto_informed_taker", "fast_structural",
    }:
        raise ContractError("live_scope_component_shadows")
    if set(scope.get("research_zero_authority_families") or []) != set(
        config["research_zero_authority_families"]
    ):
        raise ContractError("live_scope_research_zero_authority")
    if scope.get("btc_settlement_engine_contract") != \
            "config/v7_btc_settlement_engine.json":
        raise ContractError("live_scope_engine_contract_path")


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
    config: dict[str, Any], *, code_sha: str, horizon_seconds: int,
    registry: dict[str, Any],
    live_scope: dict[str, Any],
    latency_profile: dict[str, Any] | None = None,
    maker_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_config(config)
    validate_registry_authority(config, registry)
    validate_live_scope(config, live_scope)
    if SHA40.fullmatch(code_sha) is None:
        raise ContractError("exact_code_sha")
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
    research_only = horizon.get("research_only") is True
    maker_policy, taker_policy = horizon["maker"], horizon["taker"]
    maker_enabled = bool(
        not research_only and maker_policy.get("enabled") is True
        and latency["valid"] and maker["valid"]
    )
    taker_enabled = bool(
        not research_only and taker_policy.get("enabled") is True
        and latency["valid"]
    )
    blockers = sorted(set(latency_blockers + maker_blockers))
    runtime = {
        "schema": SNAPSHOT_SCHEMA,
        "exact_code_sha": code_sha,
        "config_sha256": _canonical_hash(config),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "decision_owner": "BTC_SETTLEMENT_ENGINE",
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
    parser.add_argument("--config", type=Path, default=Path("config/v7_btc_settlement_engine.json"))
    parser.add_argument("--registry", type=Path, default=Path("config/v7_strategy_registry.json"))
    parser.add_argument("--live-scope", type=Path, default=Path("config/v7_live_model_scope.json"))
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--horizon-seconds", type=int, required=True, choices=sorted(ALLOWED_HORIZONS))
    parser.add_argument("--latency-profile", type=Path)
    parser.add_argument("--maker-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = freeze(
        _json(args.config), code_sha=args.code_sha,
        horizon_seconds=args.horizon_seconds,
        registry=_json(args.registry),
        live_scope=_json(args.live_scope),
        latency_profile=_json(args.latency_profile),
        maker_evidence=_json(args.maker_evidence),
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
