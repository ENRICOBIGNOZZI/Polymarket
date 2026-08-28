#!/usr/bin/env python3
"""Deterministic V7 structural-relation registry and full-depth economics.

Discovery can propose records, but only a verified, valid record with a
deterministically checked payout matrix may reach the structural PAPER sleeve.
No semantic relationship is inferred by this module from prices.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class StructuralError(ValueError):
    pass


class RelationType(str, Enum):
    COMPLETE_SET = "COMPLETE_SET"
    NEGATIVE_RISK = "NEGATIVE_RISK"
    IMPLICATION = "IMPLICATION"
    MUTUAL_EXCLUSION = "MUTUAL_EXCLUSION"
    EXHAUSTIVE_PAIR = "EXHAUSTIVE_PAIR"


def semantic_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StructuralRelation:
    relation_id: str
    version: int
    relation_type: RelationType
    markets: tuple[str, ...]
    instruments: tuple[str, ...]
    payout_matrix: tuple[tuple[float, ...], ...]
    guaranteed_payoff_floor: float
    source: str
    semantic_source: str
    semantic_hash: str
    generation_method: str
    verified: bool
    valid: bool

    def validate(self) -> None:
        if not self.relation_id or self.version <= 0 or not self.source or not self.semantic_source:
            raise StructuralError("incomplete_relation_identity")
        if len(self.semantic_hash) != 64 or any(c not in "0123456789abcdef" for c in self.semantic_hash.lower()):
            raise StructuralError("invalid_semantic_hash")
        if len(self.instruments) < 2 or len(set(self.instruments)) != len(self.instruments):
            raise StructuralError("invalid_relation_instruments")
        if not self.markets or not self.payout_matrix:
            raise StructuralError("missing_market_or_terminal_outcomes")
        width = len(self.instruments)
        if any(len(row) != width for row in self.payout_matrix):
            raise StructuralError("payout_matrix_dimension_mismatch")
        if any(not math.isfinite(x) or x < 0.0 for row in self.payout_matrix for x in row):
            raise StructuralError("invalid_payout")
        computed = min(sum(row) for row in self.payout_matrix)
        if not math.isclose(computed, self.guaranteed_payoff_floor, abs_tol=1e-12):
            raise StructuralError("guaranteed_floor_not_derived_from_matrix")
        if self.verified and not self.valid:
            raise StructuralError("verified_invalid_relation")
        if self.generation_method.upper() == "LLM" and self.verified:
            raise StructuralError("llm_cannot_certify_relation")

    @property
    def executable(self) -> bool:
        try:
            self.validate()
        except StructuralError:
            return False
        return self.verified and self.valid


class StructuralRegistry:
    def __init__(self, relations: Iterable[StructuralRelation]):
        rows = tuple(relations)
        ids = [x.relation_id for x in rows]
        if len(ids) != len(set(ids)):
            raise StructuralError("duplicate_relation_id")
        self._relations = {x.relation_id: x for x in rows}
        index: dict[str, list[str]] = {}
        for relation in rows:
            relation.validate()
            for instrument in relation.instruments:
                index.setdefault(instrument, []).append(relation.relation_id)
        self._instrument_index = {k: tuple(sorted(v)) for k, v in index.items()}

    def affected(self, instrument: str) -> tuple[StructuralRelation, ...]:
        return tuple(self._relations[x] for x in self._instrument_index.get(instrument, ()))

    def authorize(self, relation_id: str, expected_semantic_hash: str) -> StructuralRelation:
        row = self._relations.get(relation_id)
        if row is None or not row.executable:
            raise StructuralError("relation_not_executable")
        if row.semantic_hash != expected_semantic_hash:
            raise StructuralError("semantic_hash_mismatch")
        return row


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class LegFill:
    instrument: str
    requested: float
    filled: float
    raw_cost: float
    fee: float
    slippage: float


@dataclass(frozen=True)
class StructuralPlan:
    relation_id: str
    quantity: float
    legs: tuple[LegFill, ...]
    guaranteed_payout: float
    total_cost: float
    net_profit_floor: float
    executable: bool
    blocker: str


def _walk_asks(levels: Sequence[BookLevel], quantity: float, fee_rate: float, slippage_bps: float) -> LegFill:
    if quantity <= 0.0 or fee_rate < 0.0 or slippage_bps < 0.0:
        raise StructuralError("invalid_depth_request")
    remaining, raw, stressed, filled = quantity, 0.0, 0.0, 0.0
    for row in sorted(levels, key=lambda x: x.price):
        if not (0.0 <= row.price <= 1.0) or row.quantity <= 0.0:
            raise StructuralError("invalid_book_level")
        take = min(remaining, row.quantity)
        raw += take * row.price
        stressed += take * min(1.0, row.price * (1.0 + slippage_bps / 10_000.0))
        filled += take; remaining -= take
        if remaining <= 1e-12:
            break
    fee = stressed * fee_rate
    return LegFill("", quantity, filled, raw, fee, stressed - raw)


def plan_full_depth(
    relation: StructuralRelation,
    quantity: float,
    asks: Mapping[str, Sequence[BookLevel]],
    fee_rates: Mapping[str, float],
    *,
    slippage_bps: float = 0.0,
    capital_cost: float = 0.0,
    latency_cost: float = 0.0,
) -> StructuralPlan:
    if not relation.executable:
        return StructuralPlan(relation.relation_id, quantity, (), 0.0, 0.0, -math.inf, False, "relation_not_authorized")
    legs: list[LegFill] = []
    for instrument in relation.instruments:
        if instrument not in asks or instrument not in fee_rates:
            return StructuralPlan(relation.relation_id, quantity, tuple(legs), 0.0, 0.0, -math.inf, False, "missing_depth_or_authoritative_fee")
        fill = _walk_asks(asks[instrument], quantity, float(fee_rates[instrument]), slippage_bps)
        fill = LegFill(instrument, fill.requested, fill.filled, fill.raw_cost, fill.fee, fill.slippage)
        legs.append(fill)
        if fill.filled + 1e-12 < quantity:
            return StructuralPlan(relation.relation_id, quantity, tuple(legs), 0.0, 0.0, -math.inf, False, "insufficient_full_depth")
    guaranteed = relation.guaranteed_payoff_floor * quantity
    total = sum(x.raw_cost + x.fee + x.slippage for x in legs) + max(0.0, capital_cost) + max(0.0, latency_cost)
    profit = guaranteed - total
    return StructuralPlan(relation.relation_id, quantity, tuple(legs), guaranteed, total, profit, profit > 0.0, "" if profit > 0.0 else "nonpositive_executable_floor")


def sequential_order_ev(
    permutations: Mapping[tuple[str, ...], Mapping[str, float]],
    state_pnl: Mapping[str, float],
) -> tuple[str, ...]:
    """Choose leg order using direct empirical completion-state probabilities."""
    best: tuple[float, tuple[str, ...]] | None = None
    for order, probabilities in permutations.items():
        if not order or set(probabilities) != set(state_pnl):
            raise StructuralError("joint_state_contract_incomplete")
        if any(p < 0.0 or not math.isfinite(p) for p in probabilities.values()) or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
            raise StructuralError("invalid_joint_state_distribution")
        ev = sum(probabilities[state] * state_pnl[state] for state in probabilities)
        candidate = (ev, tuple(order))
        if best is None or candidate > best:
            best = candidate
    if best is None:
        raise StructuralError("no_execution_permutation")
    return best[1]


__all__ = ["BookLevel", "LegFill", "RelationType", "StructuralError", "StructuralPlan",
           "StructuralRelation", "StructuralRegistry", "plan_full_depth", "semantic_hash",
           "sequential_order_ev"]
