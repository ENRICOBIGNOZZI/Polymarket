#!/usr/bin/env python3
"""Fail-closed V7 cross-platform semantics, books, races and PAPER shadow.

No venue is inferred from names or text similarity. A live book can enter this
pipeline only through a verified source spec and exact schema decoder. This
module has deliberately no authenticated transport or order-submission method.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class CrossVenueError(ValueError):
    pass


class EquivalenceType(str, Enum):
    EXACT_EQUIVALENT = "EXACT_EQUIVALENT"
    COMPLEMENT = "COMPLEMENT"
    CONDITIONAL_EQUIVALENT = "CONDITIONAL_EQUIVALENT"
    NEAR_EQUIVALENT = "NEAR_EQUIVALENT"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"


class BookUpdateType(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    DELTA = "DELTA"


class BookConnectionState(str, Enum):
    COLD = "COLD"
    LIVE = "LIVE"
    DISCONNECTED = "DISCONNECTED"
    RESUMING = "RESUMING"
    QUARANTINED = "QUARANTINED"


class JointRaceState(str, Enum):
    NONE = "NONE"
    A_ONLY = "A_ONLY"
    B_ONLY = "B_ONLY"
    FULL = "FULL"


@dataclass(frozen=True)
class VenueSourceSpec:
    venue: str
    official_api_uri: str
    market_data_schema: str
    schema_hash: str
    sequence_semantics: str
    reconnect_semantics: str
    verified_at_ms: int
    verified: bool

    def validate(self) -> None:
        if not all((
            self.venue, self.official_api_uri, self.market_data_schema,
            self.schema_hash, self.sequence_semantics, self.reconnect_semantics,
        )) or not self.official_api_uri.startswith(("https://", "wss://")):
            raise CrossVenueError("incomplete_venue_source_spec")
        if self.sequence_semantics != "STRICT_MONOTONIC_PER_CONTRACT":
            raise CrossVenueError("unsupported_book_sequence_semantics")
        if self.reconnect_semantics != "RESUME_AFTER_SEQUENCE":
            raise CrossVenueError("unsupported_book_reconnect_semantics")
        if self.verified_at_ms <= 0 or not self.verified:
            raise CrossVenueError("unverified_venue_source")


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: float

    def validate(self, *, allow_zero: bool = False) -> None:
        if not 0.0 <= self.price <= 1.0 or not math.isfinite(self.price):
            raise CrossVenueError("invalid_depth_level")
        if not math.isfinite(self.quantity) or self.quantity < 0.0 or (not allow_zero and self.quantity == 0.0):
            raise CrossVenueError("invalid_depth_level")


@dataclass(frozen=True)
class VenueBookEvent:
    venue: str
    contract_id: str
    sequence: int
    update_type: BookUpdateType
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    source_ts_ms: int
    receive_ts_ms: int
    schema: str
    event_id: str

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()


class VenueWireDecoder(Protocol):
    def __call__(self, payload: bytes, receive_ts_ms: int, spec: VenueSourceSpec) -> VenueBookEvent: ...


class VerifiedVenueAdapter:
    """Read-only adapter boundary for a venue's verified market-data schema."""

    def __init__(self, spec: VenueSourceSpec, decoder: VenueWireDecoder):
        spec.validate()
        if not callable(decoder):
            raise CrossVenueError("venue_decoder_required")
        self.spec = spec
        self._decoder = decoder

    def decode_book(self, payload: bytes, receive_ts_ms: int) -> VenueBookEvent:
        if not payload or receive_ts_ms <= 0:
            raise CrossVenueError("invalid_venue_wire_envelope")
        event = self._decoder(payload, receive_ts_ms, self.spec)
        if not isinstance(event, VenueBookEvent):
            raise CrossVenueError("venue_decoder_returned_wrong_type")
        if (
            event.venue != self.spec.venue or event.schema != self.spec.market_data_schema
            or event.receive_ts_ms != receive_ts_ms
        ):
            raise CrossVenueError("venue_decoder_attestation_mismatch")
        return event


@dataclass(frozen=True)
class BookTapeRecord:
    ordinal: int
    event: VenueBookEvent
    accepted: bool
    reason: str
    connection_epoch: int
    prior_sequence: int


class VenueBookTape:
    """Canonical L2 state plus append-only accepted/rejected sequence evidence."""

    def __init__(self, venue: str, contract_id: str, *, max_age_ms: int):
        if not venue or not contract_id or max_age_ms <= 0:
            raise CrossVenueError("invalid_book_tape")
        self.venue = venue
        self.contract_id = contract_id
        self.max_age_ms = int(max_age_ms)
        self.sequence = 0
        self.last_receive_ts_ms = 0
        self.connection_epoch = 0
        self.state = BookConnectionState.COLD
        self.blocker = "cold_start"
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self._records: list[BookTapeRecord] = []
        self._event_hashes: dict[str, str] = {}

    @property
    def records(self) -> tuple[BookTapeRecord, ...]:
        return tuple(self._records)

    def _record(self, event: VenueBookEvent, accepted: bool, reason: str, prior: int) -> None:
        self._records.append(BookTapeRecord(
            len(self._records) + 1, event, accepted, reason, self.connection_epoch, prior,
        ))
        if accepted:
            self._event_hashes[event.event_id] = event.payload_hash

    def _quarantine(self, event: VenueBookEvent, blocker: str, prior: int) -> None:
        self.state = BookConnectionState.QUARANTINED
        self.blocker = blocker
        self._record(event, False, blocker, prior)

    def disconnect(self, reason: str = "venue_transport_disconnect") -> None:
        if not reason:
            raise CrossVenueError("disconnect_reason_required")
        self.state = BookConnectionState.DISCONNECTED
        self.blocker = reason

    def begin_resume(self, *, resume_after_sequence: int) -> None:
        if self.state is not BookConnectionState.DISCONNECTED:
            raise CrossVenueError("book_resume_requires_disconnect")
        if resume_after_sequence != self.sequence:
            self.state = BookConnectionState.QUARANTINED
            self.blocker = "book_resume_cursor_mismatch"
            return
        self.connection_epoch += 1
        self.state = BookConnectionState.RESUMING
        self.blocker = "book_resume_pending_contiguous_event"

    def apply(self, event: VenueBookEvent, *, now_ms: int) -> None:
        prior = self.sequence
        if self.state is BookConnectionState.DISCONNECTED:
            self._quarantine(event, "book_resume_not_started", prior); return
        if self.state is BookConnectionState.QUARANTINED:
            self._quarantine(event, "book_quarantined_requires_reconnect", prior); return
        if event.venue != self.venue or event.contract_id != self.contract_id:
            self._quarantine(event, "book_identity_mismatch", prior); return
        if not event.event_id or event.sequence <= 0 or event.receive_ts_ms < event.source_ts_ms or event.receive_ts_ms > now_ms:
            self._quarantine(event, "invalid_book_envelope", prior); return
        if now_ms - event.receive_ts_ms > self.max_age_ms:
            self._quarantine(event, "stale_book", prior); return
        known_hash = self._event_hashes.get(event.event_id)
        if known_hash is not None:
            if known_hash == event.payload_hash:
                self._record(event, False, "idempotent_duplicate", prior); return
            self._quarantine(event, "conflicting_book_event_id", prior); return
        if self.sequence == 0:
            if event.update_type is not BookUpdateType.SNAPSHOT:
                self._quarantine(event, "initial_snapshot_required", prior); return
        elif event.sequence != self.sequence + 1:
            blocker = "book_gap" if event.sequence > self.sequence else "out_of_order_book_event"
            self._quarantine(event, blocker, prior); return
        for level in (*event.bids, *event.asks):
            level.validate(allow_zero=event.update_type is BookUpdateType.DELTA)
        bids = {} if event.update_type is BookUpdateType.SNAPSHOT else dict(self.bids)
        asks = {} if event.update_type is BookUpdateType.SNAPSHOT else dict(self.asks)
        for target, updates in ((bids, event.bids), (asks, event.asks)):
            for level in updates:
                if level.quantity == 0.0: target.pop(level.price, None)
                else: target[level.price] = level.quantity
        if bids and asks and max(bids) >= min(asks):
            self._quarantine(event, "crossed_or_locked_book", prior); return
        self.bids, self.asks = bids, asks
        self.sequence = event.sequence
        self.last_receive_ts_ms = event.receive_ts_ms
        self.state = BookConnectionState.LIVE
        self.blocker = ""
        self._record(event, True, "book_update", prior)

    def usable(self, now_ms: int) -> bool:
        if self.state is not BookConnectionState.LIVE or now_ms < self.last_receive_ts_ms:
            return False
        if now_ms - self.last_receive_ts_ms > self.max_age_ms:
            self.state = BookConnectionState.QUARANTINED
            self.blocker = "stale_book"
            return False
        return bool(self.bids and self.asks)

    def ask_levels(self) -> tuple[DepthLevel, ...]:
        return tuple(DepthLevel(p, self.asks[p]) for p in sorted(self.asks))


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
    outcome_semantics: str = ""
    settlement_currency: str = ""

    def semantic_payload(self, *, include_outcome: bool = True) -> dict[str, Any]:
        value = {
            "normalized_event": self.normalized_event,
            "resolution_source": self.resolution_source, "cutoff_ms": self.cutoff_ms,
            "timezone": self.timezone, "comparator": self.comparator,
            "rounding": self.rounding, "cancellation_rules": self.cancellation_rules,
            "exception_rules": self.exception_rules, "payout": self.payout,
            "rules_hash": self.rules_hash, "outcome_semantics": self.outcome_semantics,
            "settlement_currency": self.settlement_currency,
        }
        if include_outcome: value["outcome"] = self.outcome.upper()
        return value

    def validate(self) -> None:
        if not all((
            self.venue, self.contract_id, self.normalized_event, self.outcome,
            self.resolution_source, self.timezone, self.comparator, self.rounding,
            self.cancellation_rules, self.exception_rules, self.rules_hash,
            self.outcome_semantics, self.settlement_currency,
        )):
            raise CrossVenueError("incomplete_contract_semantics")
        if self.outcome.upper() not in {"YES", "NO"}:
            raise CrossVenueError("unsupported_binary_outcome")
        if self.cutoff_ms <= 0 or self.payout <= 0.0 or not math.isfinite(self.payout):
            raise CrossVenueError("invalid_contract_economics")


@dataclass(frozen=True)
class SemanticVerification:
    verifier: str
    verified_at_ms: int
    evidence_uri: str
    evidence_hash: str
    exact_fields_reviewed: tuple[str, ...]

    def validate(self) -> None:
        required_fields = {
            "normalized_event", "resolution_source", "cutoff_ms", "timezone",
            "comparator", "rounding", "cancellation_rules", "exception_rules",
            "payout", "rules_hash", "outcome_semantics", "settlement_currency",
        }
        if (
            not self.verifier or self.verified_at_ms <= 0
            or not self.evidence_uri.startswith("https://") or not self.evidence_hash
            or not required_fields.issubset(set(self.exact_fields_reviewed))
        ):
            raise CrossVenueError("incomplete_semantic_verification")


def equivalence_hash(a: CrossVenueContract, b: CrossVenueContract, kind: EquivalenceType) -> str:
    canonical = json.dumps(
        {"a": a.semantic_payload(), "b": b.semantic_payload(), "type": kind.value},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractEquivalence:
    contract_a: CrossVenueContract
    contract_b: CrossVenueContract
    equivalence_type: EquivalenceType
    semantic_hash: str
    verified: bool
    verification: SemanticVerification | None = None

    @property
    def hard_arb_authorized(self) -> bool:
        try: self.validate()
        except CrossVenueError: return False
        return self.verified and self.equivalence_type in {
            EquivalenceType.EXACT_EQUIVALENT, EquivalenceType.COMPLEMENT,
        }

    def validate(self) -> None:
        self.contract_a.validate(); self.contract_b.validate()
        if self.contract_a.venue == self.contract_b.venue:
            raise CrossVenueError("cross_venue_requires_distinct_venues")
        base_equal = self.contract_a.semantic_payload(include_outcome=False) == self.contract_b.semantic_payload(include_outcome=False)
        same_outcome = self.contract_a.outcome.upper() == self.contract_b.outcome.upper()
        complements = {self.contract_a.outcome.upper(), self.contract_b.outcome.upper()} == {"YES", "NO"}
        if self.equivalence_type is EquivalenceType.EXACT_EQUIVALENT and not (base_equal and same_outcome):
            raise CrossVenueError("false_exact_equivalence")
        if self.equivalence_type is EquivalenceType.COMPLEMENT and not (base_equal and complements):
            raise CrossVenueError("false_complement_equivalence")
        if self.semantic_hash != equivalence_hash(self.contract_a, self.contract_b, self.equivalence_type):
            raise CrossVenueError("equivalence_semantic_hash_mismatch")
        if not self.verified or self.verification is None:
            raise CrossVenueError("semantic_verification_required")
        self.verification.validate()


def classify(a: CrossVenueContract, b: CrossVenueContract) -> EquivalenceType:
    a.validate(); b.validate()
    if a.semantic_payload(include_outcome=False) != b.semantic_payload(include_outcome=False):
        return EquivalenceType.NOT_EQUIVALENT
    if a.outcome.upper() == b.outcome.upper(): return EquivalenceType.EXACT_EQUIVALENT
    if {a.outcome.upper(), b.outcome.upper()} == {"YES", "NO"}: return EquivalenceType.COMPLEMENT
    return EquivalenceType.NOT_EQUIVALENT


def depth_cost(levels: Sequence[DepthLevel], quantity: float, fee_rate: float, slippage_bps: float) -> float | None:
    """Compatibility estimator; forward evidence should use exact fee quotes."""
    if quantity <= 0.0 or fee_rate < 0.0 or slippage_bps < 0.0: raise CrossVenueError("invalid_depth_request")
    remaining, cost = quantity, 0.0
    for row in sorted(levels, key=lambda x: x.price):
        row.validate()
        take = min(remaining, row.quantity)
        cost += take * min(1.0, row.price * (1.0 + slippage_bps / 10_000.0)); remaining -= take
        if remaining <= 1e-12: break
    if remaining > 1e-12: return None
    return cost * (1.0 + fee_rate)


def _depth_notional(levels: Sequence[DepthLevel], quantity: float) -> float | None:
    if quantity <= 0.0: raise CrossVenueError("invalid_depth_request")
    remaining, cost = quantity, 0.0
    for row in sorted(levels, key=lambda x: x.price):
        row.validate()
        take = min(remaining, row.quantity)
        remaining -= take; cost += take * row.price
        if remaining <= 1e-12: break
    return None if remaining > 1e-12 else cost


@dataclass(frozen=True)
class AuthoritativeFeeQuote:
    venue: str
    contract_id: str
    schedule_id: str
    official_source_uri: str
    schedule_hash: str
    quantity: float
    notional: float
    fee_amount: float
    quoted_at_ms: int
    effective_at_ms: int
    book_sequence: int
    verified: bool

    def validate(self) -> None:
        if not all((
            self.venue, self.contract_id, self.schedule_id,
            self.official_source_uri, self.schedule_hash,
        )) or not self.official_source_uri.startswith("https://"):
            raise CrossVenueError("incomplete_fee_quote")
        values = (self.quantity, self.notional, self.fee_amount)
        if any(not math.isfinite(x) or x < 0.0 for x in values) or self.quantity <= 0.0:
            raise CrossVenueError("invalid_fee_quote")
        if (
            self.quoted_at_ms <= 0 or self.effective_at_ms > self.quoted_at_ms
            or self.book_sequence <= 0 or not self.verified
        ):
            raise CrossVenueError("unverified_fee_quote")


@dataclass(frozen=True)
class PaperBalanceSnapshot:
    venue: str
    currency: str
    available: float
    reserved: float
    as_of_ms: int
    snapshot_id: str
    paper_only: bool = True

    def validate(self) -> None:
        if not self.venue or not self.currency or not self.snapshot_id or self.as_of_ms <= 0:
            raise CrossVenueError("incomplete_balance_snapshot")
        if any(not math.isfinite(x) or x < 0.0 for x in (self.available, self.reserved)):
            raise CrossVenueError("invalid_balance_snapshot")
        if not self.paper_only:
            raise CrossVenueError("real_balance_authority_forbidden")


@dataclass(frozen=True)
class CrossVenuePlan:
    quantity: float
    guaranteed_payout: float
    expected_net_pnl: float
    return_per_capital_second: float
    executable: bool
    blocker: str
    paper_only: bool = True


@dataclass(frozen=True)
class RobustCostVector:
    fx_cost: float = 0.0
    capital_cost: float = 0.0
    settlement_risk: float = 0.0
    latency_buffer: float = 0.0
    unwind_risk: float = 0.0

    def validate(self) -> None:
        values = (
            self.fx_cost, self.capital_cost, self.settlement_risk,
            self.latency_buffer, self.unwind_risk,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise CrossVenueError("invalid_robust_cost_vector")

    @property
    def total(self) -> float:
        self.validate()
        return sum((self.fx_cost, self.capital_cost, self.settlement_risk,
                    self.latency_buffer, self.unwind_risk))


@dataclass(frozen=True)
class RobustQuantityPlan:
    quantity: float
    robust_ev: float
    base_plan: CrossVenuePlan
    cost_vector: RobustCostVector
    executable: bool
    blocker: str


def select_robust_quantity(
    candidates: Sequence[tuple[CrossVenuePlan, RobustCostVector]],
) -> RobustQuantityPlan:
    """Return q* by robust post-cost EV without interpolating missing depth."""
    if not candidates:
        raise CrossVenueError("robust_quantity_candidates_required")
    seen: set[float] = set()
    evaluated: list[RobustQuantityPlan] = []
    for plan, costs in candidates:
        costs.validate()
        if plan.quantity <= 0.0 or not math.isfinite(plan.quantity) or plan.quantity in seen:
            raise CrossVenueError("invalid_or_duplicate_robust_quantity")
        seen.add(plan.quantity)
        robust_ev = plan.expected_net_pnl - costs.total if plan.executable else -math.inf
        evaluated.append(RobustQuantityPlan(
            plan.quantity, robust_ev, plan, costs, plan.executable and robust_ev > 0.0,
            "" if plan.executable and robust_ev > 0.0 else
            (plan.blocker or "nonpositive_robust_ev"),
        ))
    best = max(evaluated, key=lambda row: (row.robust_ev, -row.quantity))
    if best.executable:
        return best
    return RobustQuantityPlan(best.quantity, best.robust_ev, best.base_plan,
                              best.cost_vector, False, best.blocker)


def _joint_inputs(
    probabilities: Mapping[str, float], adjustments: Mapping[str, float],
) -> tuple[str, ...]:
    states = tuple(x.value for x in JointRaceState)
    if set(probabilities) != set(states) or set(adjustments) != set(states):
        raise CrossVenueError("direct_joint_state_contract_required")
    if any(not math.isfinite(p) or p < 0.0 for p in probabilities.values()) or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
        raise CrossVenueError("invalid_joint_state_probabilities")
    if any(not math.isfinite(x) for x in adjustments.values()):
        raise CrossVenueError("invalid_joint_state_pnl")
    return states


def plan_cross_venue(
    equivalence: ContractEquivalence, *, quantity: float,
    asks_a: Sequence[DepthLevel], asks_b: Sequence[DepthLevel], fee_a: float, fee_b: float,
    slippage_bps: float, transfer_cost: float, execution_state_probabilities: Mapping[str, float],
    state_pnl_adjustments: Mapping[str, float], balances: Mapping[str, float], duration_seconds: float,
) -> CrossVenuePlan:
    if not equivalence.hard_arb_authorized:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "equivalence_not_authorized")
    if equivalence.equivalence_type is not EquivalenceType.COMPLEMENT:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "non_complement_legs_not_guaranteed")
    cost_a = depth_cost(asks_a, quantity, fee_a, slippage_bps)
    cost_b = depth_cost(asks_b, quantity, fee_b, slippage_bps)
    if cost_a is None or cost_b is None:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "insufficient_full_depth")
    if balances.get(equivalence.contract_a.venue, 0.0) + 1e-12 < cost_a or balances.get(equivalence.contract_b.venue, 0.0) + 1e-12 < cost_b:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "prepositioned_balance_insufficient")
    states = _joint_inputs(execution_state_probabilities, state_pnl_adjustments)
    guaranteed = quantity * min(equivalence.contract_a.payout, equivalence.contract_b.payout)
    full_net = guaranteed - cost_a - cost_b - max(0.0, transfer_cost)
    state_pnl = dict(state_pnl_adjustments); state_pnl[JointRaceState.FULL.value] += full_net
    ev = sum(execution_state_probabilities[x] * state_pnl[x] for x in states)
    capital = cost_a + cost_b
    score = ev / (capital * duration_seconds) if capital > 0.0 and duration_seconds > 0.0 else -math.inf
    return CrossVenuePlan(quantity, guaranteed, ev, score, ev > 0.0, "" if ev > 0.0 else "nonpositive_joint_execution_ev")


def plan_cross_venue_exact(
    equivalence: ContractEquivalence, *, quantity: float,
    asks_a: Sequence[DepthLevel], asks_b: Sequence[DepthLevel],
    fee_quote_a: AuthoritativeFeeQuote, fee_quote_b: AuthoritativeFeeQuote,
    balance_a: PaperBalanceSnapshot, balance_b: PaperBalanceSnapshot,
    execution_state_probabilities: Mapping[str, float], state_pnl_adjustments: Mapping[str, float],
    transfer_cost: float, duration_seconds: float,
) -> CrossVenuePlan:
    if not equivalence.hard_arb_authorized:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "equivalence_not_authorized")
    if equivalence.equivalence_type is not EquivalenceType.COMPLEMENT:
        return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "non_complement_legs_not_guaranteed")
    fee_quote_a.validate(); fee_quote_b.validate(); balance_a.validate(); balance_b.validate()
    contracts = (equivalence.contract_a, equivalence.contract_b)
    quotes = (fee_quote_a, fee_quote_b)
    balances = (balance_a, balance_b)
    levels = (asks_a, asks_b)
    costs: list[float] = []
    for contract, fee, balance, book in zip(contracts, quotes, balances, levels):
        if fee.venue != contract.venue or fee.contract_id != contract.contract_id:
            raise CrossVenueError("fee_quote_contract_mismatch")
        if balance.venue != contract.venue or balance.currency != contract.settlement_currency:
            raise CrossVenueError("balance_contract_mismatch")
        notional = _depth_notional(book, quantity)
        if notional is None:
            return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "insufficient_full_depth")
        if not math.isclose(fee.quantity, quantity, abs_tol=1e-12) or not math.isclose(fee.notional, notional, abs_tol=1e-12):
            raise CrossVenueError("fee_quote_notional_mismatch")
        cost = notional + fee.fee_amount
        if balance.available + 1e-12 < cost:
            return CrossVenuePlan(quantity, 0.0, -math.inf, -math.inf, False, "prepositioned_balance_insufficient")
        costs.append(cost)
    states = _joint_inputs(execution_state_probabilities, state_pnl_adjustments)
    guaranteed = quantity * min(x.payout for x in contracts)
    full_net = guaranteed - sum(costs) - max(0.0, transfer_cost)
    state_pnl = dict(state_pnl_adjustments); state_pnl[JointRaceState.FULL.value] += full_net
    ev = sum(execution_state_probabilities[x] * state_pnl[x] for x in states)
    capital = sum(costs)
    score = ev / (capital * duration_seconds) if capital > 0.0 and duration_seconds > 0.0 else -math.inf
    return CrossVenuePlan(quantity, guaranteed, ev, score, ev > 0.0, "" if ev > 0.0 else "nonpositive_joint_execution_ev")


@dataclass(frozen=True)
class JointRaceObservation:
    opportunity_id: str
    bundle_id: str
    decision_ts_ms: int
    deadline_ts_ms: int
    requested_quantity: float
    completed_a: float
    completed_b: float
    observed_a_ts_ms: int | None
    observed_b_ts_ms: int | None
    book_sequence_a: int
    book_sequence_b: int
    paper_only: bool = True

    def validate(self) -> None:
        if not self.opportunity_id or not self.bundle_id or self.decision_ts_ms <= 0 or self.deadline_ts_ms < self.decision_ts_ms:
            raise CrossVenueError("invalid_joint_race_identity")
        if self.requested_quantity <= 0.0 or any(
            not math.isfinite(x) or x < 0.0 or x > self.requested_quantity + 1e-12
            for x in (self.completed_a, self.completed_b)
        ):
            raise CrossVenueError("invalid_joint_race_quantity")
        if self.book_sequence_a <= 0 or self.book_sequence_b <= 0 or not self.paper_only:
            raise CrossVenueError("joint_race_requires_paper_book_lineage")
        for completed, timestamp in ((self.completed_a, self.observed_a_ts_ms), (self.completed_b, self.observed_b_ts_ms)):
            if completed > 0.0 and (timestamp is None or not self.decision_ts_ms <= timestamp <= self.deadline_ts_ms):
                raise CrossVenueError("invalid_joint_race_timestamp")

    @property
    def state(self) -> JointRaceState:
        self.validate()
        a = self.completed_a + 1e-12 >= self.requested_quantity
        b = self.completed_b + 1e-12 >= self.requested_quantity
        if a and b: return JointRaceState.FULL
        if a: return JointRaceState.A_ONLY
        if b: return JointRaceState.B_ONLY
        return JointRaceState.NONE


class JointRaceTape:
    def __init__(self):
        self._rows: list[JointRaceObservation] = []
        self._opportunity_ids: set[str] = set()

    @property
    def rows(self) -> tuple[JointRaceObservation, ...]:
        return tuple(self._rows)

    def append(self, observation: JointRaceObservation) -> None:
        observation.validate()
        if observation.opportunity_id in self._opportunity_ids:
            raise CrossVenueError("duplicate_joint_race_opportunity")
        self._opportunity_ids.add(observation.opportunity_id)
        self._rows.append(observation)

    def probabilities(self, *, minimum_independent_bundles: int = 1) -> dict[str, float]:
        bundles = {row.bundle_id for row in self._rows}
        if len(bundles) < minimum_independent_bundles or not self._rows:
            raise CrossVenueError("insufficient_joint_race_evidence")
        # One bundle contributes one independent observation: retain the latest
        # recorded race for a repeated opportunity cluster.
        latest: dict[str, JointRaceObservation] = {}
        for row in self._rows: latest[row.bundle_id] = row
        counts = {state.value: 0 for state in JointRaceState}
        for row in latest.values(): counts[row.state.value] += 1
        total = len(latest)
        return {state: count / total for state, count in counts.items()}


class CrossPlatformForwardShadow:
    """Durable forward evidence with explicit PAPER-only authority."""

    def __init__(self, path: Path, *, run_id: str):
        if not run_id:
            raise CrossVenueError("shadow_run_id_required")
        self.path = Path(path)
        self.run_id = run_id

    def record(
        self, *, equivalence: ContractEquivalence, plan: CrossVenuePlan,
        observed_at_ms: int, book_sequence_a: int, book_sequence_b: int,
        fee_quote_a: AuthoritativeFeeQuote, fee_quote_b: AuthoritativeFeeQuote,
        balance_a: PaperBalanceSnapshot, balance_b: PaperBalanceSnapshot,
        race: JointRaceObservation | None = None,
    ) -> str:
        if observed_at_ms <= 0 or not plan.paper_only:
            raise CrossVenueError("only_timestamped_paper_plans_can_be_recorded")
        equivalence.validate(); fee_quote_a.validate(); fee_quote_b.validate()
        balance_a.validate(); balance_b.validate()
        if book_sequence_a != fee_quote_a.book_sequence or book_sequence_b != fee_quote_b.book_sequence:
            raise CrossVenueError("shadow_book_fee_lineage_mismatch")
        if race is not None:
            race.validate()
            if race.book_sequence_a != book_sequence_a or race.book_sequence_b != book_sequence_b:
                raise CrossVenueError("shadow_race_book_lineage_mismatch")
        payload: dict[str, Any] = {
            "schema": "v7_cross_platform_forward_shadow_v1", "run_id": self.run_id,
            "observed_at_ms": observed_at_ms, "paper_only": True,
            "real_order_submission": False, "semantic_hash": equivalence.semantic_hash,
            "equivalence_type": equivalence.equivalence_type.value,
            "book_sequence_a": book_sequence_a, "book_sequence_b": book_sequence_b,
            "fee_schedule_a": fee_quote_a.schedule_id, "fee_schedule_b": fee_quote_b.schedule_id,
            "balance_snapshot_a": balance_a.snapshot_id, "balance_snapshot_b": balance_b.snapshot_id,
            "plan": asdict(plan), "race": asdict(race) if race else None,
        }
        if race is not None: payload["race_state"] = race.state.value
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope = json.dumps({**payload, "record_hash": record_hash}, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(envelope + "\n")
            handle.flush(); os.fsync(handle.fileno())
        return record_hash


__all__ = [
    "AuthoritativeFeeQuote", "BookConnectionState", "BookTapeRecord", "BookUpdateType",
    "ContractEquivalence", "CrossPlatformForwardShadow", "CrossVenueContract",
    "CrossVenueError", "CrossVenuePlan", "DepthLevel", "EquivalenceType",
    "JointRaceObservation", "JointRaceState", "JointRaceTape", "PaperBalanceSnapshot",
    "RobustCostVector", "RobustQuantityPlan",
    "SemanticVerification", "VenueBookEvent", "VenueBookTape", "VenueSourceSpec",
    "VenueWireDecoder", "VerifiedVenueAdapter", "classify", "depth_cost",
    "equivalence_hash", "plan_cross_venue", "plan_cross_venue_exact", "select_robust_quantity",
]
