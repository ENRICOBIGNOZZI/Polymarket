#!/usr/bin/env python3
"""Causal, source-aware V7 OSINT probability-update kernel.

The module emits research/shadow decisions only.  LLM-derived fields are
allowed for extraction and proposal, never for mapping verification, sizing or
execution authority.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Iterable, Mapping, Sequence


EVENT_FAMILIES = {
    "ELECTION_RESULT", "COURT_RULING", "REGULATORY_APPROVAL", "REGULATORY_REJECTION",
    "COMPANY_ANNOUNCEMENT", "POLICY_ANNOUNCEMENT", "RESIGNATION", "APPOINTMENT",
    "WEATHER_EVENT", "SPORTS_RESULT", "DEADLINE_MET", "DEADLINE_MISSED",
}


class OsintError(ValueError):
    pass


class SourceTier(IntEnum):
    PRIMARY = 1
    VERIFIED_PROVIDER = 2
    SECONDARY_MEDIA = 3
    UNVERIFIED = 4


def _logit(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise OsintError("probability_out_of_range")
    return math.log(p / (1.0 - p))


def _logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x); return 1.0 / (1.0 + z)
    z = math.exp(x); return z / (1.0 + z)


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    event_family: str
    entity: str
    source_id: str
    source_tier: SourceTier
    source_event_id: str
    root_lineage_id: str
    published_ts_ms: int
    received_ts_ms: int
    payload_hash: str
    correction_of: str = ""
    extracted_by_llm: bool = False

    def validate(self, decision_ts_ms: int) -> None:
        if not all((self.event_id, self.entity, self.source_id, self.source_event_id,
                    self.root_lineage_id, self.payload_hash)):
            raise OsintError("incomplete_event_lineage")
        if self.event_family not in EVENT_FAMILIES:
            raise OsintError("unknown_event_family")
        if self.published_ts_ms <= 0 or self.received_ts_ms < self.published_ts_ms:
            raise OsintError("invalid_event_clocks")
        if self.received_ts_ms > decision_ts_ms:
            raise OsintError("future_event_used")


@dataclass(frozen=True)
class EventMarketLink:
    event_family: str
    market_family: str
    direction: int
    causal_mechanism: str
    mapping_version: int
    verified: bool
    verification_method: str

    def validate(self) -> None:
        if self.event_family not in EVENT_FAMILIES or not self.market_family or self.direction not in {-1, 1}:
            raise OsintError("invalid_event_market_link")
        if not self.causal_mechanism or self.mapping_version <= 0:
            raise OsintError("incomplete_event_market_link")
        if self.verified and self.verification_method.upper() == "LLM":
            raise OsintError("llm_cannot_verify_mapping")


@dataclass(frozen=True)
class LikelihoodModel:
    event_family: str
    log_likelihood_ratio: float
    independent_events: int
    trained_until_ms: int
    frozen: bool
    oos_validated: bool

    def validate(self, event: RawEvent) -> None:
        if self.event_family != event.event_family or not math.isfinite(self.log_likelihood_ratio):
            raise OsintError("likelihood_model_mismatch")
        if self.independent_events <= 0 or self.trained_until_ms <= 0 or not self.frozen:
            raise OsintError("invalid_likelihood_model")
        if self.trained_until_ms >= event.published_ts_ms:
            raise OsintError("likelihood_training_leakage")


@dataclass(frozen=True)
class OsintDecision:
    event_id: str
    market_id: str
    prior: float
    posterior: float
    posterior_lower: float
    posterior_upper: float
    action: str
    side: str
    shadow_only: bool
    blockers: tuple[str, ...]
    independent_lineage_count: int


def deduplicate(events: Iterable[RawEvent]) -> tuple[RawEvent, ...]:
    """One canonical event per root lineage; corrections replace older rows."""
    by_lineage: dict[str, RawEvent] = {}
    for event in sorted(events, key=lambda x: (x.received_ts_ms, x.event_id)):
        current = by_lineage.get(event.root_lineage_id)
        if current is None or event.correction_of == current.event_id:
            by_lineage[event.root_lineage_id] = event
    return tuple(sorted(by_lineage.values(), key=lambda x: (x.received_ts_ms, x.event_id)))


def corroboration_count(event: RawEvent, independent_events: Sequence[RawEvent]) -> int:
    sources = {
        x.source_id for x in independent_events
        if x.event_family == event.event_family and x.entity == event.entity
        and x.root_lineage_id != event.root_lineage_id and x.source_tier <= SourceTier.SECONDARY_MEDIA
    }
    return len(sources)


def update_probability(
    *, market_id: str, prior: float, event: RawEvent, link: EventMarketLink,
    model: LikelihoodModel, decision_ts_ms: int, independent_events: Sequence[RawEvent] = (),
    uncertainty_log_odds: float = 0.0, pm_bid: float | None = None, pm_ask: float | None = None,
    executable_cost: float = 0.0, minimum_edge: float = 0.0,
) -> OsintDecision:
    blockers: list[str] = []
    try: event.validate(decision_ts_ms)
    except OsintError as exc: blockers.append(str(exc))
    try: link.validate()
    except OsintError as exc: blockers.append(str(exc))
    try: model.validate(event)
    except OsintError as exc: blockers.append(str(exc))
    if link.event_family != event.event_family: blockers.append("event_mapping_mismatch")
    if not link.verified: blockers.append("mapping_not_verified")
    if not model.oos_validated: blockers.append("likelihood_not_oos_validated")
    corroboration = corroboration_count(event, independent_events)
    if event.source_tier >= SourceTier.SECONDARY_MEDIA and corroboration < 1:
        blockers.append("secondary_source_not_independently_corroborated")
    if event.source_tier == SourceTier.UNVERIFIED: blockers.append("unverified_source")
    if uncertainty_log_odds < 0.0 or not math.isfinite(uncertainty_log_odds): blockers.append("invalid_uncertainty")
    signed_llr = link.direction * model.log_likelihood_ratio
    posterior = _logistic(_logit(prior) + signed_llr)
    lower = _logistic(_logit(prior) + signed_llr - abs(uncertainty_log_odds))
    upper = _logistic(_logit(prior) + signed_llr + abs(uncertainty_log_odds))
    if pm_bid is None or pm_ask is None or not (0.0 <= pm_bid <= pm_ask <= 1.0):
        blockers.append("valid_executable_book_missing"); action, side = "NOTHING", ""
    elif lower - pm_ask - executable_cost > minimum_edge:
        action, side = "TAKE", "YES"
    elif pm_bid - upper - executable_cost > minimum_edge:
        action, side = "TAKE", "NO"
    elif not (lower <= pm_ask and pm_bid <= upper):
        action, side = "CANCEL", "BOTH"
    else:
        action, side = "NOTHING", ""
    if blockers: action, side = "NOTHING", ""
    return OsintDecision(event.event_id, market_id, prior, posterior, lower, upper, action, side,
                         True, tuple(sorted(set(blockers))), 1 + corroboration)


def edge_half_life(samples: Mapping[float, float]) -> float | None:
    """First elapsed second where absolute edge is at most half initial edge."""
    rows = sorted((float(t), abs(float(edge))) for t, edge in samples.items() if t >= 0 and math.isfinite(edge))
    if not rows or rows[0][1] <= 0.0:
        return None
    threshold = rows[0][1] / 2.0
    return next((t for t, edge in rows if edge <= threshold), None)


__all__ = ["EVENT_FAMILIES", "EventMarketLink", "LikelihoodModel", "OsintDecision", "OsintError",
           "RawEvent", "SourceTier", "corroboration_count", "deduplicate", "edge_half_life",
           "update_probability"]
