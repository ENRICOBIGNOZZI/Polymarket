#!/usr/bin/env python3
"""Fail-closed cold-start fair hierarchy for new Polymarket contracts."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Mapping, Sequence


class MarketOpenError(ValueError):
    pass


class FairSource(IntEnum):
    DETERMINISTIC_RELATION = 1
    EXTERNAL_SETTLEMENT_MODEL = 2
    RELATED_MATURE_MARKETS = 3
    BASE_RATE = 4


@dataclass(frozen=True)
class ColdStartContract:
    market_id: str
    event_id: str
    rules_hash: str
    settlement_source: str
    comparator: str
    cutoff_ms: int
    timezone: str
    verified: bool

    def validate(self) -> None:
        if not all((self.market_id, self.event_id, self.rules_hash, self.settlement_source,
                    self.comparator, self.timezone)) or self.cutoff_ms <= 0:
            raise MarketOpenError("unknown_or_incomplete_semantics")
        if not self.verified:
            raise MarketOpenError("unverified_semantics")


@dataclass(frozen=True)
class FairEstimate:
    source: FairSource
    probability: float
    uncertainty: float
    model_version: str
    causal_ts_ms: int
    verified: bool

    def validate(self, decision_ts_ms: int) -> None:
        if not 0.0 < self.probability < 1.0 or not 0.0 <= self.uncertainty <= 0.5:
            raise MarketOpenError("invalid_fair_estimate")
        if not self.model_version or self.causal_ts_ms <= 0 or self.causal_ts_ms > decision_ts_ms:
            raise MarketOpenError("invalid_fair_lineage")
        if not self.verified:
            raise MarketOpenError("unverified_fair_source")


@dataclass(frozen=True)
class OpenDecision:
    market_id: str
    action: str
    fair: float | None
    lower: float | None
    upper: float | None
    size_multiplier: float
    side: str
    shadow_only: bool
    blocker: str


def select_fair(estimates: Sequence[FairEstimate], decision_ts_ms: int) -> FairEstimate:
    valid: list[FairEstimate] = []
    for row in estimates:
        try: row.validate(decision_ts_ms)
        except MarketOpenError: continue
        valid.append(row)
    if not valid: raise MarketOpenError("no_verified_initial_fair")
    return min(valid, key=lambda x: (int(x.source), x.uncertainty, x.model_version))


def decide_open(
    contract: ColdStartContract, estimates: Sequence[FairEstimate], *, decision_ts_ms: int,
    open_ts_ms: int, pm_bid: float, pm_ask: float, executable_cost: float,
    minimum_edge: float, base_size_multiplier: float = 0.25,
) -> OpenDecision:
    try:
        contract.validate()
        if open_ts_ms <= 0 or open_ts_ms > decision_ts_ms: raise MarketOpenError("invalid_open_clock")
        if not 0.0 <= pm_bid <= pm_ask <= 1.0: raise MarketOpenError("invalid_open_book")
        fair = select_fair(estimates, decision_ts_ms)
        age_s = (decision_ts_ms - open_ts_ms) / 1_000.0
        cold_uncertainty = min(0.49, fair.uncertainty * 1.5 + 0.02 * math.exp(-age_s / 60.0))
        lower, upper = max(0.0, fair.probability - cold_uncertainty), min(1.0, fair.probability + cold_uncertainty)
        if lower - pm_ask - executable_cost > minimum_edge:
            action, side = "TAKE", "YES"
        elif pm_bid - upper - executable_cost > minimum_edge:
            action, side = "TAKE", "NO"
        elif pm_ask - pm_bid >= 2.0 * (cold_uncertainty + executable_cost + minimum_edge):
            action, side = "MAKE", "BOTH"
        else:
            action, side = "NOTHING", ""
        size = min(1.0, max(0.0, base_size_multiplier)) * min(1.0, age_s / 300.0 + 0.1) * max(0.0, 1.0 - 2.0 * cold_uncertainty)
        return OpenDecision(contract.market_id, action, fair.probability, lower, upper, size, side, True, "")
    except MarketOpenError as exc:
        return OpenDecision(contract.market_id, "NOTHING", None, None, None, 0.0, "", True, str(exc))


def edge_decay(open_ts_ms: int, observations: Mapping[int, float]) -> tuple[tuple[float, float], ...]:
    if open_ts_ms <= 0: raise MarketOpenError("invalid_open_clock")
    rows = []
    for ts_ms, edge in observations.items():
        if ts_ms < open_ts_ms or not math.isfinite(float(edge)): raise MarketOpenError("invalid_edge_decay_observation")
        rows.append(((ts_ms - open_ts_ms) / 1_000.0, float(edge)))
    return tuple(sorted(rows))


__all__ = ["ColdStartContract", "FairEstimate", "FairSource", "MarketOpenError", "OpenDecision",
           "decide_open", "edge_decay", "select_fair"]
