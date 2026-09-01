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
ENGINE_COMPONENTS = {
    "BTC_SETTLEMENT_ENGINE": {
        "crypto_settlement_fair", "crypto_informed_taker", "professional_maker",
    },
    "STRUCTURAL_ARB_ENGINE": {"hard_arb", "fast_structural"},
}
ENGINE_ACTIONS = {
    "BTC_SETTLEMENT_ENGINE": {"MAKE", "TAKE", "CANCEL", "NOTHING"},
    "STRUCTURAL_ARB_ENGINE": {"ARB", "CANCEL", "NOTHING"},
}
NEW_RISK_ACTIONS = {"MAKE", "TAKE", "ARB"}
SAFE_ACTIONS = {"CANCEL", "NOTHING"}
COST_FIELDS = (
    "fee", "slippage", "unwind_loss", "capital_cost", "latency_cost",
    "adverse_markout", "rebate",
)
AUTHORITY_STATES = {"AUTHORITATIVE", "CONSERVATIVE_BOUND", "CONSERVATIVE_ZERO"}


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

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "OpportunityEnvelope":
        required = {
            "schema", "version", "model_sha", "config_hash", "policy_hash", "run_id",
            "source_snapshot_identity", "engine_id", "component_provenance", "market_id",
            "event_id", "contract_id", "mapping_identity", "action", "side",
            "decision_receive_timestamp_ns", "source_event_timestamps_ns", "fair_value",
            "conservative_expected_wealth_change", "cost_vector", "cost_authority",
            "uncertainty", "calibration_status", "latency", "capacity",
            "inventory_delta", "portfolio_exposure_delta", "settlement", "eligible",
            "reasons", "deterministic_replay_key", "expires_at_ns",
        }
        if not isinstance(value, dict) or set(value) != required:
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
        components = value.get("component_provenance")
        if (
            not isinstance(components, list) or not components
            or len(components) != len(set(components))
            or not set(components) <= ENGINE_COMPONENTS[engine_id]
        ):
            raise OpportunityError("component_provenance")
        if value.get("side") not in {"BUY", "SELL", "YES", "NO", "NONE", "MULTI"}:
            raise OpportunityError("side")
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
        settlement = _mapping(value.get("settlement"), "settlement")
        if set(settlement) != {"definition", "source", "verified"}:
            raise OpportunityError("settlement_fields")
        reasons = value.get("reasons")
        if not isinstance(reasons, list) or not reasons or any(not isinstance(x, str) or not x for x in reasons):
            raise OpportunityError("reasons")
        if not isinstance(value.get("eligible"), bool):
            raise OpportunityError("eligible")
        if action in NEW_RISK_ACTIONS and (
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
        "selected_replay_key": None,
        "new_risk_authorized": False,
        "reasons": reasons or ["FAIL_CLOSED"],
    }


def coordinate(
    raw_envelopes: Iterable[dict[str, Any]], *, now_ns: int,
    new_risk_authorized: bool = False,
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
    risk = [row for row in live if row.action == "CANCEL"]
    if risk:
        selected = min(risk, key=lambda row: row.replay_key)
        return {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "decision_timestamp_ns": int(now_ns),
            "action": "CANCEL",
            "selected_replay_key": selected.replay_key,
            "new_risk_authorized": False,
            "reasons": ["RISK_ACTION_PREEMPTS_ALPHA"],
        }
    candidates = [row for row in live if row.action in NEW_RISK_ACTIONS]
    if not new_risk_authorized:
        return fail_closed_decision(now_ns=now_ns, reasons=["NEW_RISK_NOT_AUTHORIZED"])
    positive = [row for row in candidates if row.expected_wealth_change > 0.0]
    if not positive:
        return fail_closed_decision(now_ns=now_ns, reasons=["NO_POSITIVE_CONSERVATIVE_WEALTH_CHANGE"])
    selected = max(positive, key=lambda row: (row.expected_wealth_change, row.replay_key))
    return {
        "schema": "polymarket_v7_global_opportunity_decision_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "decision_timestamp_ns": int(now_ns),
        "action": selected.action,
        "selected_replay_key": selected.replay_key,
        "new_risk_authorized": True,
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
