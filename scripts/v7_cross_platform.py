#!/usr/bin/env python3
"""Fail-closed V7 cross-venue equivalence and execution economics."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class CrossVenueError(ValueError):
    pass


class EquivalenceType(str, Enum):
    EXACT_EQUIVALENT = "EXACT_EQUIVALENT"
    COMPLEMENT = "COMPLEMENT"
    CONDITIONAL_EQUIVALENT = "CONDITIONAL_EQUIVALENT"
    NEAR_EQUIVALENT = "NEAR_EQUIVALENT"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"


@dataclass(frozen=True)
class CrossVenueContract:
    venue: str
    contract_id: str
    normalized_event: str
    outcome: str
    resolution_source: str
    cutoff_ms: int
    timezone: str
    comparator: str
    rounding: str
    cancellation_rules: str
    exception_rules: str
    payout: float
    rules_hash: str

    def semantic_payload(self, *, include_outcome: bool = True) -> dict[str, Any]:
        value = {
            "normalized_event": self.normalized_event, "resolution_source": self.resolution_source,
            "cutoff_ms": self.cutoff_ms, "timezone": self.timezone, "comparator": self.comparator,
            "rounding": self.rounding, "cancellation_rules": self.cancellation_rules,
            "exception_rules": self.exception_rules, "payout": self.payout, "rules_hash": self.rules_hash,
        }
        if include_outcome: value["outcome"] = self.outcome.upper()
        return value

    def validate(self) -> None:
        if not all((self.venue, self.contract_id, self.normalized_event, self.outcome,
                    self.resolution_source, self.timezone, self.comparator, self.rounding,
                    self.cancellation_rules, self.exception_rules, self.rules_hash)):
            raise CrossVenueError("incomplete_contract_semantics")
        if self.cutoff_ms <= 0 or self.payout <= 0.0 or not math.isfinite(self.payout):
            raise CrossVenueError("invalid_contract_economics")


@dataclass(frozen=True)
class ContractEquivalence:
    contract_a: CrossVenueContract
    contract_b: CrossVenueContract
    equivalence_type: EquivalenceType
    semantic_hash: str
    verified: bool

    @property
    def hard_arb_authorized(self) -> bool:
        try: self.validate()
        except CrossVenueError: return False
        return self.verified and self.equivalence_type in {EquivalenceType.EXACT_EQUIVALENT, EquivalenceType.COMPLEMENT}

    def validate(self) -> None:
        self.contract_a.validate(); self.contract_b.validate()
        if self.contract_a.venue == self.contract_b.venue:
            raise CrossVenueError("cross_venue_requires_distinct_venues")
        base_equal = self.contract_a.semantic_payload(include_outcome=False) == self.contract_b.semantic_payload(include_outcome=False)
        same_outcome = self.contract_a.outcome.upper() == self.contract_b.outcome.upper()
        complements = {self.contract_a.outcome.upper(), self.contract_b.outcome.upper()} == {"YES", "NO"}
        deterministic = (base_equal and same_outcome, base_equal and complements)
        if self.equivalence_type is EquivalenceType.EXACT_EQUIVALENT and not deterministic[0]:
            raise CrossVenueError("false_exact_equivalence")
        if self.equivalence_type is EquivalenceType.COMPLEMENT and not deterministic[1]:
            raise CrossVenueError("false_complement_equivalence")
        canonical = json.dumps({"a": self.contract_a.semantic_payload(), "b": self.contract_b.semantic_payload(),
                                "type": self.equivalence_type.value}, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.semantic_hash != expected:
            raise CrossVenueError("equivalence_semantic_hash_mismatch")


def classify(a: CrossVenueContract, b: CrossVenueContract) -> EquivalenceType:
    a.validate(); b.validate()
    if a.semantic_payload(include_outcome=False) != b.semantic_payload(include_outcome=False):
        return EquivalenceType.NOT_EQUIVALENT
    if a.outcome.upper() == b.outcome.upper(): return EquivalenceType.EXACT_EQUIVALENT
    if {a.outcome.upper(), b.outcome.upper()} == {"YES", "NO"}: return EquivalenceType.COMPLEMENT
    return EquivalenceType.NOT_EQUIVALENT


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: float


def depth_cost(levels: Sequence[DepthLevel], quantity: float, fee_rate: float, slippage_bps: float) -> float | None:
    if quantity <= 0.0 or fee_rate < 0.0 or slippage_bps < 0.0: raise CrossVenueError("invalid_depth_request")
    remaining, cost = quantity, 0.0
    for row in sorted(levels, key=lambda x: x.price):
        if not 0.0 <= row.price <= 1.0 or row.quantity <= 0.0: raise CrossVenueError("invalid_depth_level")
        take = min(remaining, row.quantity)
        cost += take * min(1.0, row.price * (1.0 + slippage_bps / 10_000.0)); remaining -= take
        if remaining <= 1e-12: break
    if remaining > 1e-12: return None
    return cost * (1.0 + fee_rate)


@dataclass(frozen=True)
class CrossVenuePlan:
    quantity: float
    guaranteed_payout: float
    expected_net_pnl: float
    return_per_capital_second: float
    executable: bool
    blocker: str


def plan_cross_venue(
    equivalence: ContractEquivalence, *, quantity: float,
    asks_a: Sequence[DepthLevel], asks_b: Sequence[DepthLevel], fee_a: float, fee_b: float,
    slippage_bps: float, transfer_cost: float, execution_state_probabilities: Mapping[str, float],
    state_pnl_adjustments: Mapping[str, float], balances: Mapping[str, float], duration_seconds: float,
) -> CrossVenuePlan:
    if not equivalence.hard_arb_authorized:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "equivalence_not_authorized")
    cost_a = depth_cost(asks_a, quantity, fee_a, slippage_bps)
    cost_b = depth_cost(asks_b, quantity, fee_b, slippage_bps)
    if cost_a is None or cost_b is None:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "insufficient_full_depth")
    if balances.get(equivalence.contract_a.venue, 0.0) + 1e-12 < cost_a or balances.get(equivalence.contract_b.venue, 0.0) + 1e-12 < cost_b:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "prepositioned_balance_insufficient")
    states = {"NONE", "A_ONLY", "B_ONLY", "FULL"}
    if set(execution_state_probabilities) != states or set(state_pnl_adjustments) != states:
        raise CrossVenueError("direct_joint_state_contract_required")
    if any(p < 0.0 for p in execution_state_probabilities.values()) or not math.isclose(sum(execution_state_probabilities.values()), 1.0, abs_tol=1e-9):
        raise CrossVenueError("invalid_joint_state_probabilities")
    guaranteed = quantity * min(equivalence.contract_a.payout, equivalence.contract_b.payout)
    full_net = guaranteed - cost_a - cost_b - max(0.0, transfer_cost)
    state_pnl = dict(state_pnl_adjustments); state_pnl["FULL"] += full_net
    ev = sum(execution_state_probabilities[x] * state_pnl[x] for x in states)
    capital = cost_a + cost_b
    score = ev / (capital * duration_seconds) if capital > 0.0 and duration_seconds > 0.0 else -math.inf
    return CrossVenuePlan(quantity, guaranteed, ev, score, ev > 0.0, "" if ev > 0.0 else "nonpositive_joint_execution_ev")


__all__ = ["ContractEquivalence", "CrossVenueContract", "CrossVenueError", "CrossVenuePlan",
           "DepthLevel", "EquivalenceType", "classify", "depth_cost", "plan_cross_venue"]
