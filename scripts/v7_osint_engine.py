#!/usr/bin/env python3
"""Causal, source-aware V7 OSINT probability-update kernel.

The module emits research/shadow decisions only.  LLM-derived fields are
allowed for extraction and proposal, never for mapping verification, sizing or
execution authority.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


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


class Transport(str, Enum):
    STREAM = "STREAM"
    SSE = "SSE"
    WEBSOCKET = "WEBSOCKET"
    API = "API"
    RSS = "RSS"
    CONDITIONAL_HTTP = "CONDITIONAL_HTTP"


@dataclass(frozen=True)
class CollectorSource:
    source_id: str
    entity: str
    family: str
    authority_tier: SourceTier
    endpoint: str
    transport: Transport
    expected_latency_ms: int
    historical_reliability: float
    correction_rate: float
    availability: float
    event_types: tuple[str, ...]
    parser: str
    adapter_version: str
    max_age_ms: int
    statistics_status: str
    enabled: bool = True

    def validate(self) -> None:
        if not all((self.source_id, self.entity, self.family, self.endpoint, self.parser,
                    self.adapter_version)):
            raise OsintError("incomplete_information_source")
        if not self.endpoint.startswith("https://"):
            raise OsintError("source_endpoint_must_be_https")
        if self.expected_latency_ms <= 0 or self.max_age_ms <= 0:
            raise OsintError("invalid_source_latency_contract")
        if not 0.0 <= self.historical_reliability <= 1.0:
            raise OsintError("invalid_source_reliability")
        if not 0.0 <= self.correction_rate <= 1.0 or not 0.0 <= self.availability <= 1.0:
            raise OsintError("invalid_source_operating_statistics")
        if not self.event_types or not set(self.event_types).issubset(EVENT_FAMILIES):
            raise OsintError("invalid_source_event_types")
        if self.parser not in {"FEDERAL_REGISTER_JSON", "ATOM", "RSS"}:
            raise OsintError("unsupported_source_parser")
        if self.statistics_status not in {"PRIOR_UNVALIDATED", "EMPIRICAL"}:
            raise OsintError("invalid_source_statistics_status")
        if self.enabled and self.authority_tier > SourceTier.VERIFIED_PROVIDER:
            raise OsintError("enabled_source_must_be_authoritative")


@dataclass(frozen=True)
class CollectorSourceCatalog:
    sources: tuple[CollectorSource, ...]
    schema: str = "polymarket_v7_osint_source_registry_v1"

    @classmethod
    def load(cls, path: Path) -> "CollectorSourceCatalog":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema") != cls.schema:
            raise OsintError("source_registry_schema_mismatch")
        if (value.get("paper_only") is not True
                or value.get("authenticated_execution") is not False
                or value.get("real_order_submission") is not False):
            raise OsintError("source_registry_safety_contract_mismatch")
        rows: list[CollectorSource] = []
        for raw in value.get("sources", []):
            try:
                row = CollectorSource(
                    source_id=str(raw["source_id"]), entity=str(raw["entity"]),
                    family=str(raw["family"]), authority_tier=SourceTier[str(raw["authority_tier"])],
                    endpoint=str(raw["endpoint"]), transport=Transport(str(raw["transport"])),
                    expected_latency_ms=int(raw["expected_latency_ms"]),
                    historical_reliability=float(raw["historical_reliability"]),
                    correction_rate=float(raw["correction_rate"]), availability=float(raw["availability"]),
                    event_types=tuple(str(x) for x in raw["event_types"]), parser=str(raw["parser"]),
                    adapter_version=str(raw["adapter_version"]),
                    max_age_ms=int(raw["max_age_ms"]), statistics_status=str(raw["statistics_status"]),
                    enabled=bool(raw.get("enabled", True)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise OsintError("invalid_source_registry_row") from exc
            row.validate(); rows.append(row)
        if not rows or len({x.source_id for x in rows}) != len(rows):
            raise OsintError("source_registry_empty_or_duplicate")
        return cls(tuple(rows))

    def enabled(self) -> tuple[CollectorSource, ...]:
        return tuple(x for x in self.sources if x.enabled)

    def get(self, source_id: str) -> CollectorSource:
        matches = [x for x in self.sources if x.source_id == source_id]
        if len(matches) != 1:
            raise OsintError("source_not_registered")
        return matches[0]


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
    content: str = ""
    transport: str = ""
    connection_epoch: int = 0

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
        if self.transport and self.transport not in {x.value for x in Transport}:
            raise OsintError("invalid_event_transport")
        if self.connection_epoch < 0:
            raise OsintError("invalid_connection_epoch")


@dataclass(frozen=True)
class StructuredEvent:
    event_id: str
    entity: str
    event_family: str
    attributes: Mapping[str, Any]
    effective_ts_ms: int
    announcement_ts_ms: int
    received_ts_ms: int
    source_tier: SourceTier
    corroboration: int
    novelty: float
    confidence: float
    root_lineage_id: str
    valid: bool


def normalize_event(
    event: RawEvent, source: CollectorSource, *, decision_ts_ms: int,
    attributes: Mapping[str, Any] | None = None, effective_ts_ms: int | None = None,
    corroboration: int = 0, novelty: float = 1.0, confidence: float | None = None,
) -> StructuredEvent:
    event.validate(decision_ts_ms); source.validate()
    if event.source_id != source.source_id or event.source_tier != source.authority_tier:
        raise OsintError("source_registry_identity_mismatch")
    if event.event_family not in source.event_types:
        raise OsintError("source_not_authorized_for_event_family")
    if not 0.0 <= novelty <= 1.0 or corroboration < 0:
        raise OsintError("invalid_event_normalization")
    inferred_confidence = source.historical_reliability * (1.0 - source.correction_rate)
    final_confidence = inferred_confidence if confidence is None else float(confidence)
    if not 0.0 <= final_confidence <= 1.0:
        raise OsintError("invalid_event_confidence")
    effective = event.published_ts_ms if effective_ts_ms is None else int(effective_ts_ms)
    if effective <= 0 or effective > decision_ts_ms:
        raise OsintError("invalid_event_effective_time")
    return StructuredEvent(
        event.event_id, event.entity, event.event_family, dict(attributes or {}), effective,
        event.published_ts_ms, event.received_ts_ms, event.source_tier, corroboration,
        novelty, final_confidence, event.root_lineage_id, True,
    )


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
class LikelihoodObservation:
    event_family: str
    root_lineage_id: str
    observed_ts_ms: int
    event_direction: int
    outcome: int
    prior: float

    def validate(self) -> None:
        if self.event_family not in EVENT_FAMILIES or not self.root_lineage_id or self.observed_ts_ms <= 0:
            raise OsintError("invalid_likelihood_observation")
        if self.event_direction not in {-1, 1} or self.outcome not in {0, 1} or not 0.0 < self.prior < 1.0:
            raise OsintError("invalid_likelihood_observation")


@dataclass(frozen=True)
class CalibrationReport:
    independent_events: int
    brier: float
    baseline_brier: float
    log_loss: float
    calibration_intercept: float
    calibration_slope: float
    ece: float
    improves_baseline: bool


def _unique_likelihood_rows(rows: Iterable[LikelihoodObservation]) -> tuple[LikelihoodObservation, ...]:
    unique: dict[str, LikelihoodObservation] = {}
    for row in sorted(rows, key=lambda x: (x.observed_ts_ms, x.root_lineage_id)):
        row.validate()
        current = unique.get(row.root_lineage_id)
        if current is not None and (current.outcome != row.outcome or current.event_family != row.event_family):
            raise OsintError("conflicting_independent_event_label")
        unique[row.root_lineage_id] = row
    return tuple(sorted(unique.values(), key=lambda x: (x.observed_ts_ms, x.root_lineage_id)))


def fit_likelihood_model(
    event_family: str, rows: Iterable[LikelihoodObservation], *, trained_until_ms: int,
    alpha: float = 1.0, oos_rows: Iterable[LikelihoodObservation] = (), minimum_oos_events: int = 20,
    embargo_ms: int = 0,
) -> tuple[LikelihoodModel, CalibrationReport | None]:
    if event_family not in EVENT_FAMILIES or trained_until_ms <= 0 or alpha <= 0.0 or embargo_ms < 0:
        raise OsintError("invalid_likelihood_fit_request")
    train = tuple(x for x in _unique_likelihood_rows(rows)
                  if x.event_family == event_family and x.observed_ts_ms <= trained_until_ms)
    if not train:
        raise OsintError("likelihood_fit_requires_independent_events")
    positive = [x for x in train if x.outcome == 1]
    negative = [x for x in train if x.outcome == 0]
    if not positive or not negative:
        raise OsintError("likelihood_fit_requires_both_outcomes")
    p_event_given_yes = (sum(x.event_direction > 0 for x in positive) + alpha) / (len(positive) + 2.0 * alpha)
    p_event_given_no = (sum(x.event_direction > 0 for x in negative) + alpha) / (len(negative) + 2.0 * alpha)
    llr = math.log(p_event_given_yes / p_event_given_no)
    provisional = LikelihoodModel(event_family, llr, len(train), trained_until_ms, True, False)
    test = tuple(x for x in _unique_likelihood_rows(oos_rows)
                 if x.event_family == event_family and x.observed_ts_ms > trained_until_ms + embargo_ms)
    if {x.root_lineage_id for x in train}.intersection(x.root_lineage_id for x in test):
        raise OsintError("likelihood_oos_lineage_overlap")
    report = calibration_report(test, llr) if test else None
    validated = bool(report and report.independent_events >= minimum_oos_events and report.improves_baseline)
    return replace(provisional, oos_validated=validated), report


def calibration_report(rows: Iterable[LikelihoodObservation], log_likelihood_ratio: float) -> CalibrationReport:
    values = _unique_likelihood_rows(rows)
    if not values or not math.isfinite(log_likelihood_ratio):
        raise OsintError("calibration_requires_observations")
    predictions = [
        _logistic(_logit(x.prior) + x.event_direction * log_likelihood_ratio) for x in values
    ]
    labels = [float(x.outcome) for x in values]
    eps = 1e-12
    brier = sum((p - y) ** 2 for p, y in zip(predictions, labels)) / len(values)
    baseline = sum((x.prior - y) ** 2 for x, y in zip(values, labels)) / len(values)
    logloss = -sum(y * math.log(max(eps, p)) + (1.0-y) * math.log(max(eps, 1.0-p))
                   for p, y in zip(predictions, labels)) / len(values)
    zs = [_logit(min(1.0-eps, max(eps, p))) for p in predictions]
    intercept, slope = 0.0, 1.0
    for _ in range(50):
        fitted = [_logistic(intercept + slope * z) for z in zs]
        gradient_a = sum(y - p for y, p in zip(labels, fitted))
        gradient_b = sum((y - p) * z for y, p, z in zip(labels, fitted, zs))
        info_aa = sum(p * (1.0-p) for p in fitted) + 1e-8
        info_ab = sum(p * (1.0-p) * z for p, z in zip(fitted, zs))
        info_bb = sum(p * (1.0-p) * z * z for p, z in zip(fitted, zs)) + 1e-8
        determinant = info_aa * info_bb - info_ab * info_ab
        if determinant <= 1e-14:
            break
        delta_a = (info_bb * gradient_a - info_ab * gradient_b) / determinant
        delta_b = (-info_ab * gradient_a + info_aa * gradient_b) / determinant
        intercept += max(-2.0, min(2.0, delta_a))
        slope += max(-2.0, min(2.0, delta_b))
        if max(abs(delta_a), abs(delta_b)) < 1e-8:
            break
    bins: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for p, y in zip(predictions, labels): bins[min(9, int(p * 10.0))].append((p, y))
    ece = sum(len(group) / len(values) * abs(sum(p for p, _ in group) / len(group) -
              sum(y for _, y in group) / len(group)) for group in bins if group)
    return CalibrationReport(len(values), brier, baseline, logloss, intercept, slope, ece,
                             brier + 1e-12 < baseline)


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


__all__ = ["EVENT_FAMILIES", "CalibrationReport", "CollectorSource", "CollectorSourceCatalog", "EventMarketLink",
           "LikelihoodModel", "LikelihoodObservation", "OsintDecision", "OsintError", "RawEvent",
           "SourceTier", "StructuredEvent", "Transport", "calibration_report",
           "corroboration_count", "deduplicate", "edge_half_life", "fit_likelihood_model",
           "normalize_event", "update_probability"]
