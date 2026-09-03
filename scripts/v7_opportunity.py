#!/usr/bin/env python3
"""Typed V7 opportunity contract and single global portfolio coordinator."""
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_opportunity_envelope_v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
HASH = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
HASH64 = re.compile(r"^[0-9a-f]{64}$")
ENGINE_COMPONENTS = {
    "CRYPTO_SETTLEMENT_ENGINE": {
        "crypto_settlement_fair", "crypto_informed_taker", "professional_maker",
    },
    "STRUCTURAL_ARB_ENGINE": {"hard_arb", "fast_structural"},
}
ENGINE_ACTIONS = {
    "CRYPTO_SETTLEMENT_ENGINE": {"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"},
    "STRUCTURAL_ARB_ENGINE": {"ARB", "CANCEL", "NOTHING"},
}
NEW_RISK_ACTIONS = {"MAKE", "TAKE", "ARB"}
SAFE_ACTIONS = {"CANCEL", "WITHDRAW", "NOTHING"}
COST_FIELDS = (
    "fee", "slippage", "unwind_loss", "capital_cost", "latency_cost",
    "adverse_markout", "rebate",
)
AUTHORITY_STATES = {"AUTHORITATIVE", "CONSERVATIVE_BOUND", "CONSERVATIVE_ZERO"}
CRYPTO_ASSETS = {"BTC", "ETH", "SOL", "XRP"}
CRYPTO_HORIZONS = {"M1", "M5", "M15", "H1", "H4"}
CRYPTO_CONTEXT_FIELDS = {
    "asset", "horizon", "contract_family", "settlement_semantic_hash",
    "authority", "research_only",
}


class OpportunityError(ValueError):
    pass


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpportunityError(name)
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise OpportunityError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OpportunityError(name) from exc
    if not math.isfinite(number):
        raise OpportunityError(name)
    return number


@dataclass(frozen=True)
class OpportunityEnvelope:
    raw: dict[str, Any]

    @property
    def engine_id(self) -> str:
        return str(self.raw["engine_id"])

    @property
    def action(self) -> str:
        return str(self.raw["action"])

    @property
    def expected_wealth_change(self) -> float:
        return float(self.raw["conservative_expected_wealth_change"])

    @property
    def expires_at_ns(self) -> int:
        return int(self.raw["expires_at_ns"])

    @property
    def replay_key(self) -> str:
        return str(self.raw["deterministic_replay_key"])

    @property
    def eligible(self) -> bool:
        return self.raw["eligible"] is True

    @property
    def is_probe(self) -> bool:
        exploration = self.raw.get("exploration")
        return isinstance(exploration, dict) and exploration.get("mode") == "PAPER_BOOTSTRAP_PROBE"

    @property
    def probe_point_wealth_change(self) -> float:
        exploration = self.raw.get("exploration")
        return float(exploration["point_expected_wealth_change"]) if isinstance(exploration, dict) else float("-inf")

    @property
    def probe_information_score(self) -> float:
        exploration = self.raw.get("exploration")
        return float(exploration["information_score"]) if isinstance(exploration, dict) else float("-inf")

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "OpportunityEnvelope":
        required = {
            "schema", "version", "model_sha", "config_hash", "policy_hash", "run_id",
            "source_snapshot_identity", "engine_id", "component_provenance", "market_id",
            "event_id", "contract_id", "mapping_identity", "crypto_context", "action", "side",
            "decision_receive_timestamp_ns", "source_event_timestamps_ns", "fair_value",
            "conservative_expected_wealth_change", "cost_vector", "cost_authority",
            "uncertainty", "calibration_status", "latency", "capacity",
            "execution_plan",
            "inventory_delta", "portfolio_exposure_delta", "settlement", "eligible",
            "reasons", "deterministic_replay_key", "expires_at_ns",
        }
        optional = {"exploration"}
        if not isinstance(value, dict) or frozenset(value) not in {frozenset(required), frozenset(required | optional)}:
            raise OpportunityError("field_partition")
        if value.get("schema") != SCHEMA or value.get("version") != 1:
            raise OpportunityError("schema")
        if not SHA.fullmatch(str(value.get("model_sha") or "")):
            raise OpportunityError("model_sha")
        if not HASH.fullmatch(str(value.get("config_hash") or "")) or not HASH.fullmatch(
            str(value.get("policy_hash") or "")
        ):
            raise OpportunityError("config_or_policy_hash")
        for key in (
            "run_id", "source_snapshot_identity", "market_id", "event_id",
            "contract_id", "mapping_identity", "deterministic_replay_key",
        ):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise OpportunityError(key)
        engine_id = str(value.get("engine_id") or "")
        action = str(value.get("action") or "")
        if engine_id not in ENGINE_COMPONENTS or action not in ENGINE_ACTIONS[engine_id]:
            raise OpportunityError("engine_action")
        crypto_context = value.get("crypto_context")
        if engine_id == "CRYPTO_SETTLEMENT_ENGINE":
            context = _mapping(crypto_context, "crypto_context")
            if set(context) != CRYPTO_CONTEXT_FIELDS:
                raise OpportunityError("crypto_context_fields")
            if (
                context.get("asset") not in CRYPTO_ASSETS
                or context.get("horizon") not in CRYPTO_HORIZONS
                or not isinstance(context.get("contract_family"), str)
                or not context["contract_family"]
                or not HASH64.fullmatch(str(context.get("settlement_semantic_hash") or ""))
                or context.get("authority") not in {"SHADOW", "SHADOW_ZERO_AUTHORITY", "PAPER_EXPLORATION", "PAPER"}
                or not isinstance(context.get("research_only"), bool)
            ):
                raise OpportunityError("crypto_context_identity")
            if action in NEW_RISK_ACTIONS and (
                context["research_only"] is True
                or context["authority"] == "SHADOW_ZERO_AUTHORITY"
            ):
                raise OpportunityError("crypto_context_zero_authority")
        elif crypto_context is not None:
            raise OpportunityError("structural_crypto_context_forbidden")
        components = value.get("component_provenance")
        if (
            not isinstance(components, list) or not components
            or len(components) != len(set(components))
            or not set(components) <= ENGINE_COMPONENTS[engine_id]
        ):
            raise OpportunityError("component_provenance")
        if value.get("side") not in {"BUY", "SELL", "YES", "NO", "NONE", "MULTI"}:
            raise OpportunityError("side")
        if action in SAFE_ACTIONS and value.get("side") != "NONE":
            raise OpportunityError("safe_action_side")
        decision_ns = int(value.get("decision_receive_timestamp_ns") or 0)
        source_ns = value.get("source_event_timestamps_ns")
        expires_ns = int(value.get("expires_at_ns") or 0)
        if (
            decision_ns <= 0 or not isinstance(source_ns, list) or not source_ns
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in source_ns)
            or max(source_ns) > decision_ns or expires_ns <= decision_ns
        ):
            raise OpportunityError("causal_timestamps_or_ttl")
        fair = _mapping(value.get("fair_value"), "fair_value")
        if set(fair) != {"lower", "point", "upper"}:
            raise OpportunityError("fair_value_fields")
        lower, point, upper = (_finite(fair[name], f"fair_value:{name}") for name in ("lower", "point", "upper"))
        if not 0.0 <= lower <= point <= upper <= 1.0:
            raise OpportunityError("fair_value_bounds")
        _finite(value.get("conservative_expected_wealth_change"), "expected_wealth_change")
        _finite(value.get("inventory_delta"), "inventory_delta")
        _finite(value.get("portfolio_exposure_delta"), "portfolio_exposure_delta")
        costs = _mapping(value.get("cost_vector"), "cost_vector")
        authority = _mapping(value.get("cost_authority"), "cost_authority")
        if set(costs) != set(COST_FIELDS) or set(authority) != set(COST_FIELDS):
            raise OpportunityError("cost_vector_complete")
        for name in COST_FIELDS:
            cost = _finite(costs[name], f"cost:{name}")
            if cost < 0.0:
                raise OpportunityError(f"cost_negative:{name}")
            if authority[name] not in AUTHORITY_STATES:
                raise OpportunityError(f"cost_authority:{name}")
        if authority["rebate"] != "AUTHORITATIVE" and float(costs["rebate"]) != 0.0:
            raise OpportunityError("unauthoritative_rebate_nonzero")
        uncertainty = _mapping(value.get("uncertainty"), "uncertainty")
        if set(uncertainty) != {"lower_bound", "upper_bound", "status"}:
            raise OpportunityError("uncertainty_fields")
        if uncertainty.get("status") not in {"MATURE", "IMMATURE", "MISSING"}:
            raise OpportunityError("uncertainty_status")
        if _finite(uncertainty["lower_bound"], "uncertainty_lower") > _finite(
            uncertainty["upper_bound"], "uncertainty_upper"
        ):
            raise OpportunityError("uncertainty_bounds")
        if value.get("calibration_status") not in {"MATURE", "IMMATURE", "MISSING", "NOT_APPLICABLE"}:
            raise OpportunityError("calibration_status")
        latency = _mapping(value.get("latency"), "latency")
        if set(latency) != {"profile_id", "profile_valid", "economic_percentile", "arrival_ns"}:
            raise OpportunityError("latency_fields")
        if latency.get("economic_percentile") not in {"p90", "p95", "p99", "p99.9"}:
            raise OpportunityError("latency_percentile")
        if int(latency.get("arrival_ns") or -1) < 0:
            raise OpportunityError("latency_arrival")
        capacity = _mapping(value.get("capacity"), "capacity")
        if set(capacity) != {"executable_size", "depth_provenance"}:
            raise OpportunityError("capacity_fields")
        if _finite(capacity.get("executable_size"), "executable_size") < 0.0:
            raise OpportunityError("executable_size")
        execution = _mapping(value.get("execution_plan"), "execution_plan")
        if set(execution) != {
            "atomic_unit_id", "execution_style", "legs", "partial_fill_plan",
            "timeout_ms", "unwind_plan",
        }:
            raise OpportunityError("execution_plan_fields")
        if not isinstance(execution.get("atomic_unit_id"), str) or not execution["atomic_unit_id"]:
            raise OpportunityError("atomic_unit_id")
        if execution.get("execution_style") not in {"SINGLE_LEG", "SEQUENTIAL_ATOMIC_INTENT"}:
            raise OpportunityError("execution_style")
        if execution.get("partial_fill_plan") not in {
            "CANCEL_REMAINDER", "COMPLETE_OR_UNWIND", "NO_NEW_RISK",
        } or execution.get("unwind_plan") not in {
            "NONE", "FULL_DEPTH_BOUNDED_UNWIND", "CANCEL_ONLY",
        }:
            raise OpportunityError("partial_fill_or_unwind_plan")
        timeout_ms = execution.get("timeout_ms")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise OpportunityError("execution_timeout")
        legs = execution.get("legs")
        if not isinstance(legs, list) or not legs:
            raise OpportunityError("execution_legs")
        leg_ids: set[str] = set()
        for leg in legs:
            if not isinstance(leg, dict) or set(leg) != {
                "leg_id", "market_id", "contract_id", "token_id", "side",
                "target_quantity", "limit_price", "fee_authority",
            }:
                raise OpportunityError("execution_leg_fields")
            leg_id = leg.get("leg_id")
            if not isinstance(leg_id, str) or not leg_id or leg_id in leg_ids:
                raise OpportunityError("execution_leg_identity")
            leg_ids.add(leg_id)
            for name in ("market_id", "contract_id", "token_id"):
                if not isinstance(leg.get(name), str) or not leg[name]:
                    raise OpportunityError(f"execution_leg:{name}")
            if leg.get("side") not in {"BUY", "SELL"}:
                raise OpportunityError("execution_leg:side")
            if _finite(leg.get("target_quantity"), "execution_leg:quantity") <= 0.0:
                raise OpportunityError("execution_leg:quantity")
            price = _finite(leg.get("limit_price"), "execution_leg:price")
            if not 0.0 <= price <= 1.0:
                raise OpportunityError("execution_leg:price")
            if leg.get("fee_authority") not in AUTHORITY_STATES:
                raise OpportunityError("execution_leg:fee_authority")
        if action == "ARB" and (
            len(legs) < 2
            or execution.get("execution_style") != "SEQUENTIAL_ATOMIC_INTENT"
            or execution.get("partial_fill_plan") != "COMPLETE_OR_UNWIND"
            or execution.get("unwind_plan") != "FULL_DEPTH_BOUNDED_UNWIND"
        ):
            raise OpportunityError("structural_atomic_execution_plan")
        settlement = _mapping(value.get("settlement"), "settlement")
        if set(settlement) != {"definition", "source", "verified"}:
            raise OpportunityError("settlement_fields")
        reasons = value.get("reasons")
        if not isinstance(reasons, list) or not reasons or any(not isinstance(x, str) or not x for x in reasons):
            raise OpportunityError("reasons")
        if not isinstance(value.get("eligible"), bool):
            raise OpportunityError("eligible")
        probe = value.get("exploration")
        if probe is not None:
            probe = _mapping(probe, "exploration")
            if set(probe) != {
                "mode", "point_expected_wealth_change", "maximum_probe_loss",
                "probe_loss_cap", "information_score", "promotion_eligible",
                "robust_candidate", "arrival_revalidated", "model_id", "model_hash",
            }:
                raise OpportunityError("exploration_fields")
            point_change = _finite(probe.get("point_expected_wealth_change"), "probe_point_expected_wealth_change")
            maximum_loss = _finite(probe.get("maximum_probe_loss"), "probe_maximum_loss")
            loss_cap = _finite(probe.get("probe_loss_cap"), "probe_loss_cap")
            information_score = _finite(probe.get("information_score"), "probe_information_score")
            if (
                probe.get("mode") != "PAPER_BOOTSTRAP_PROBE"
                or point_change <= 0.0
                or maximum_loss <= 0.0
                or loss_cap <= 0.0
                or maximum_loss > loss_cap + 1e-9
                or loss_cap > 2.0 + 1e-9
                or information_score <= 0.0
                or probe.get("promotion_eligible") is not False
                or probe.get("robust_candidate") is not False
                or probe.get("arrival_revalidated") is not True
                or probe.get("model_id") != "btc_m5_same_oracle_diffusion_bootstrap_v1"
                or not HASH64.fullmatch(str(probe.get("model_hash") or ""))
                or _finite(value.get("conservative_expected_wealth_change"), "expected_wealth_change") < -maximum_loss - 1e-9
                or _finite(value.get("portfolio_exposure_delta"), "portfolio_exposure_delta") > loss_cap + 1e-9
                or engine_id != "CRYPTO_SETTLEMENT_ENGINE"
                or action != "TAKE"
                or not isinstance(crypto_context, dict)
                or crypto_context.get("authority") != "PAPER_EXPLORATION"
                or crypto_context.get("asset") != "BTC"
                or crypto_context.get("horizon") != "M5"
            ):
                raise OpportunityError("paper_exploration_probe_invalid")

        exploration = (
            engine_id == "CRYPTO_SETTLEMENT_ENGINE"
            and action in {"MAKE", "TAKE"}
            and isinstance(crypto_context, dict)
            and crypto_context.get("authority") == "PAPER_EXPLORATION"
        )
        if exploration:
            if (
                crypto_context.get("asset") != "BTC"
                or crypto_context.get("horizon") != "M5"
                or crypto_context.get("research_only") is not False
                or uncertainty.get("status") not in {"IMMATURE", "MATURE"}
                or value.get("calibration_status") not in {"IMMATURE", "MATURE", "NOT_APPLICABLE"}
                or settlement.get("verified") is not True
                or float(capacity["executable_size"]) <= 0.0
                or int(latency.get("arrival_ns") or -1) < 0
            ):
                raise OpportunityError("paper_exploration_evidence_incomplete")
        elif action in NEW_RISK_ACTIONS and (
            latency.get("profile_valid") is not True
            or uncertainty.get("status") != "MATURE"
            or value.get("calibration_status") not in {"MATURE", "NOT_APPLICABLE"}
            or settlement.get("verified") is not True
            or float(capacity["executable_size"]) <= 0.0
        ):
            raise OpportunityError("new_risk_evidence_incomplete")
        return cls(json.loads(json.dumps(value, sort_keys=True)))


def fail_closed_decision(*, now_ns: int, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema": "polymarket_v7_global_opportunity_decision_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "decision_timestamp_ns": int(now_ns),
        "action": "NOTHING",
        "engine_id": None,
        "crypto_context": None,
        "selected_replay_key": None,
        "new_risk_authorized": False,
        "paper_exploration_authorized": False,
        "paper_exploration_probe_authorized": False,
        "reasons": reasons or ["FAIL_CLOSED"],
    }


def coordinate(
    raw_envelopes: Iterable[dict[str, Any]], *, now_ns: int,
    new_risk_authorized: bool = False,
    paper_exploration_authorized: bool = False,
) -> dict[str, Any]:
    parsed: list[OpportunityEnvelope] = []
    errors: list[str] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(raw_envelopes):
        try:
            envelope = OpportunityEnvelope.parse(raw)
            if envelope.replay_key in seen_keys:
                raise OpportunityError("duplicate_replay_key")
            seen_keys.add(envelope.replay_key)
            parsed.append(envelope)
        except (OpportunityError, TypeError, ValueError) as exc:
            errors.append(f"INVALID_ENVELOPE:{index}:{exc}")
    if errors:
        return fail_closed_decision(now_ns=now_ns, reasons=errors)
    live = [row for row in parsed if row.expires_at_ns >= now_ns and row.eligible]
    risk = [row for row in live if row.action in {"CANCEL", "WITHDRAW"}]
    if risk:
        selected = min(risk, key=lambda row: row.replay_key)
        return {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "decision_timestamp_ns": int(now_ns),
            "action": selected.action,
            "engine_id": selected.engine_id,
            "crypto_context": selected.raw["crypto_context"],
            "selected_replay_key": selected.replay_key,
            "new_risk_authorized": False,
            "paper_exploration_authorized": False,
            "paper_exploration_probe_authorized": False,
            "reasons": ["RISK_ACTION_PREEMPTS_ALPHA"],
        }
    candidates = [row for row in live if row.action in NEW_RISK_ACTIONS]
    exploration_candidates = [
        row for row in candidates
        if row.engine_id == "CRYPTO_SETTLEMENT_ENGINE"
        and isinstance(row.raw.get("crypto_context"), dict)
        and row.raw["crypto_context"].get("authority") == "PAPER_EXPLORATION"
    ]
    if not new_risk_authorized:
        if not paper_exploration_authorized:
            return fail_closed_decision(now_ns=now_ns, reasons=["NEW_RISK_NOT_AUTHORIZED"])
        positive = [
            row for row in exploration_candidates
            if not row.is_probe and row.expected_wealth_change > 0.0
        ]
        if positive:
            selected = max(positive, key=lambda row: (row.expected_wealth_change, row.replay_key))
            return {
                "schema": "polymarket_v7_global_opportunity_decision_v1",
                "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
                "decision_timestamp_ns": int(now_ns),
                "action": selected.action,
                "engine_id": selected.engine_id,
                "crypto_context": selected.raw["crypto_context"],
                "selected_replay_key": selected.replay_key,
                "new_risk_authorized": False,
                "paper_exploration_authorized": True,
                "paper_exploration_probe_authorized": False,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "real_capital_at_risk": False,
                "reasons": ["PAPER_EXPLORATION_MAX_CONSERVATIVE_EXPECTED_ACCOUNT_WEALTH_CHANGE"],
            }
        probes = [row for row in exploration_candidates if row.is_probe]
        if not probes:
            return fail_closed_decision(now_ns=now_ns, reasons=["NO_POSITIVE_PAPER_EXPLORATION_WEALTH_CHANGE"])
        selected = max(
            probes,
            key=lambda row: (
                row.probe_information_score,
                row.probe_point_wealth_change,
                row.replay_key,
            ),
        )
        return {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "decision_timestamp_ns": int(now_ns),
            "action": selected.action,
            "engine_id": selected.engine_id,
            "crypto_context": selected.raw["crypto_context"],
            "selected_replay_key": selected.replay_key,
            "new_risk_authorized": False,
            "paper_exploration_authorized": True,
            "paper_exploration_probe_authorized": True,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "probe": selected.raw["exploration"],
            "reasons": ["PAPER_EXPLORATION_INFORMATION_GAIN_PROBE"],
        }
    positive = [row for row in candidates if row.expected_wealth_change > 0.0]
    if not positive:
        return fail_closed_decision(now_ns=now_ns, reasons=["NO_POSITIVE_CONSERVATIVE_WEALTH_CHANGE"])
    selected = max(positive, key=lambda row: (row.expected_wealth_change, row.replay_key))
    return {
        "schema": "polymarket_v7_global_opportunity_decision_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "decision_timestamp_ns": int(now_ns),
        "action": selected.action,
        "engine_id": selected.engine_id,
        "crypto_context": selected.raw["crypto_context"],
        "selected_replay_key": selected.replay_key,
        "new_risk_authorized": True,
        "paper_exploration_authorized": False,
        "paper_exploration_probe_authorized": False,
        "reasons": ["MAX_CONSERVATIVE_EXPECTED_ACCOUNT_WEALTH_CHANGE"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSON array of envelopes")
    parser.add_argument("--now-ns", type=int, required=True)
    parser.add_argument("--new-risk-authorized", action="store_true")
    args = parser.parse_args()
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise OpportunityError("input_not_array")
        print(json.dumps(coordinate(
            value, now_ns=args.now_ns,
            new_risk_authorized=args.new_risk_authorized,
        ), sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, OpportunityError) as exc:
        print(json.dumps(fail_closed_decision(
            now_ns=args.now_ns, reasons=[f"INPUT_ERROR:{exc}"],
        ), sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
