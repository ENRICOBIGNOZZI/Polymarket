#!/usr/bin/env python3
"""Offline Decimal reference for V7 native aggressive PAPER execution.

The production counterpart is simulate_taker_paper. This module is not an
executor, allocator or ledger writer and is not imported by the frozen BTC
runtime. Unknown observations remain unknown; all fills remain simulated.
"""
from __future__ import annotations
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP
import hashlib
import json
from typing import Any, Iterable, Mapping

D = Decimal
ZERO = D(0)
ASSETS = frozenset(('BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB'))
HORIZONS = frozenset(('M5', 'M15'))
ROUNDING = {'DOWN': ROUND_DOWN, 'HALF_EVEN': ROUND_HALF_EVEN, 'HALF_UP': ROUND_HALF_UP}


class ReplayError(ValueError):
    """Input cannot support the requested execution/economic conclusion."""


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ReplayError('INVALID_DECIMAL')
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ReplayError('INVALID_DECIMAL') from exc
    if not result.is_finite():
        raise ReplayError('NONFINITE_DECIMAL')
    return result


def primitive(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, '__dataclass_fields__'):
        return primitive(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(primitive(value), sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def positive_int(value: Any, reason: str, *, allow_zero: bool = False) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ReplayError(reason)


def nonempty(value: Any, reason: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(reason)


@dataclass(frozen=True)
class Clock:
    host_id: str
    boot_id: str
    monotonic_ns: int
    wall_ns: int

    def __post_init__(self) -> None:
        nonempty(self.host_id, 'HOST_MISSING')
        nonempty(self.boot_id, 'BOOT_MISSING')
        positive_int(self.monotonic_ns, 'CLOCK_INVALID')
        positive_int(self.wall_ns, 'CLOCK_INVALID')

    def after(self, nanoseconds: int) -> 'Clock':
        positive_int(nanoseconds, 'NEGATIVE_DURATION', allow_zero=True)
        return replace(self, monotonic_ns=self.monotonic_ns + nanoseconds,
                       wall_ns=self.wall_ns + nanoseconds)

    def same_domain(self, other: 'Clock') -> bool:
        return (self.host_id, self.boot_id) == (other.host_id, other.boot_id)


@dataclass(frozen=True)
class FeeTerms:
    """Explicit market terms; no default rate, incidence or rounding claim.

    Public L2 does not identify counterparties. Per-price-level fee rounding
    is a frozen simulation convention, not an exchange execution observation.
    BUY_SHARES is explicit and never charged again to cash.
    """
    market_id: str
    version: str
    rate: Decimal
    exponent: int
    currency: str
    incidence: str
    quantum: Decimal
    rounding: str
    observed_ns: int
    expires_ns: int
    source: str
    verified: bool
    share_quantum: Decimal = D('0.000001')

    def __post_init__(self) -> None:
        for name in ('market_id', 'version', 'currency', 'source'):
            nonempty(getattr(self, name), 'FEE_IDENTITY_MISSING')
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate < 0:
            raise ReplayError('FEE_RATE_INVALID')
        if type(self.exponent) is not int or not 0 <= self.exponent <= 8:
            raise ReplayError('FEE_EXPONENT_UNSUPPORTED')
        if self.incidence not in ('CASH', 'BUY_SHARES') or self.rounding not in ROUNDING:
            raise ReplayError('FEE_SEMANTICS_UNKNOWN')
        for value in (self.quantum, self.share_quantum):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ReplayError('FEE_PRECISION_INVALID')
        positive_int(self.observed_ns, 'FEE_CLOCK_INVALID')
        if type(self.expires_ns) is not int or self.expires_ns < self.observed_ns:
            raise ReplayError('FEE_CLOCK_INVALID')
        if type(self.verified) is not bool:
            raise ReplayError('FEE_VERIFICATION_INVALID')

    def charge(self, price: Decimal, quantity: Decimal) -> Decimal:
        raw = quantity * self.rate * (price * (1 - price)) ** self.exponent
        return (raw / self.quantum).to_integral_value(rounding=ROUNDING[self.rounding]) * self.quantum

    def shares_charge(self, cash_equivalent: Decimal, price: Decimal) -> Decimal:
        return (cash_equivalent / price / self.share_quantum).to_integral_value(
            rounding=ROUNDING[self.rounding]) * self.share_quantum


@dataclass(frozen=True)
class BookCut:
    """One coherent received snapshot from the existing PM-book data plane."""
    market_id: str
    token_id: str
    snapshot_id: str
    epoch: str
    generation: int
    rules_hash: str
    fee_version: str
    tick: Decimal
    min_size: Decimal
    clock: Clock
    transport_monotonic_ns: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    valid: bool

    def __post_init__(self) -> None:
        for name in ('market_id', 'token_id', 'snapshot_id', 'epoch', 'rules_hash', 'fee_version'):
            nonempty(getattr(self, name), 'BOOK_IDENTITY_MISSING')
        positive_int(self.generation, 'REGISTRY_GENERATION_INVALID')
        positive_int(self.transport_monotonic_ns, 'TRANSPORT_CLOCK_INVALID')
        if self.transport_monotonic_ns < self.clock.monotonic_ns:
            raise ReplayError('TRANSPORT_BEFORE_BOOK')
        for value in (self.tick, self.min_size):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ReplayError('BOOK_CONSTRAINTS_INVALID')
        if self.tick > 1 or type(self.valid) is not bool:
            raise ReplayError('BOOK_CONSTRAINTS_INVALID')
        for levels, reverse in ((self.bids, True), (self.asks, False)):
            if not isinstance(levels, tuple):
                raise ReplayError('MUTABLE_BOOK')
            prices = []
            for p, q in levels:
                if not isinstance(p, Decimal) or not isinstance(q, Decimal):
                    raise ReplayError('BOOK_DECIMALS_REQUIRED')
                if not p.is_finite() or not q.is_finite() or not 0 < p < 1 or q <= 0:
                    raise ReplayError('BOOK_LEVEL_INVALID')
                if p % self.tick != 0:
                    raise ReplayError('OFF_TICK_BOOK')
                prices.append(p)
            if prices != sorted(set(prices), reverse=reverse):
                raise ReplayError('BOOK_LEVEL_ORDER_INVALID')
        if self.bids and self.asks and self.bids[0][0] >= self.asks[0][0]:
            raise ReplayError('PM_BOOK_CROSSED')


@dataclass(frozen=True)
class ArrivalCut:
    book: BookCut
    fee: FeeTerms
    available_monotonic_ns: int
    market_close_wall_ns: int
    accepting_orders: bool
    rules_verified: bool
    reference_ready: bool
    oracle_ready: bool
    required_feeds_ready: bool
    kill: bool
    cutover_drain: bool
    accounting_ready: bool
    matching_delay_ns: int

    def __post_init__(self) -> None:
        positive_int(self.available_monotonic_ns, 'CUT_CLOCK_INVALID')
        positive_int(self.market_close_wall_ns, 'CLOSE_CLOCK_INVALID')
        positive_int(self.matching_delay_ns, 'MATCHING_DELAY_INVALID', allow_zero=True)
        if self.available_monotonic_ns < max(self.book.transport_monotonic_ns, self.fee.observed_ns):
            raise ReplayError('CUT_BEFORE_INPUT')
        for name in ('accepting_orders', 'rules_verified', 'reference_ready', 'oracle_ready',
                     'required_feeds_ready', 'kill', 'cutover_drain', 'accounting_ready'):
            if type(getattr(self, name)) is not bool:
                raise ReplayError('CUT_BOOLEAN_REQUIRED:' + name)


@dataclass(frozen=True)
class ReplayIntent:
    trace_id: str
    experiment_id: str
    protocol_hash: str
    signal_id: str
    parent_shock_id: str
    asset: str
    horizon: str
    market_id: str
    token_id: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    clock: Clock
    signal_monotonic_ns: int
    expires_monotonic_ns: int
    signal_expires_monotonic_ns: int
    min_tte_ns: int
    max_tte_ns: int
    max_transport_age_ns: int
    generation: int
    epoch: str
    rules_hash: str
    fee_version: str
    tick: Decimal
    matching_delay_ns: int
    decision_snapshot_id: str

    def __post_init__(self) -> None:
        for name in ('trace_id', 'experiment_id', 'protocol_hash', 'signal_id', 'parent_shock_id',
                     'market_id', 'token_id', 'epoch', 'rules_hash', 'fee_version', 'decision_snapshot_id'):
            nonempty(getattr(self, name), 'INTENT_IDENTITY_MISSING')
        if self.asset not in ASSETS:
            raise ReplayError('UNKNOWN_ASSET')
        if self.horizon not in HORIZONS:
            raise ReplayError('UNSUPPORTED_HORIZON')
        if self.side not in ('BUY', 'SELL'):
            raise ReplayError('SIDE_INVALID')
        if any(not isinstance(v, Decimal) or not v.is_finite()
               for v in (self.quantity, self.limit_price, self.tick)):
            raise ReplayError('INTENT_DECIMALS_REQUIRED')
        if self.quantity <= 0 or not 0 < self.limit_price < 1 or not 0 < self.tick <= 1:
            raise ReplayError('INTENT_PRICE_OR_SIZE_INVALID')
        if self.limit_price % self.tick != 0:
            raise ReplayError('LIMIT_OFF_TICK')
        for value in (self.signal_monotonic_ns, self.expires_monotonic_ns,
                      self.signal_expires_monotonic_ns, self.max_transport_age_ns, self.generation):
            positive_int(value, 'INTENT_CLOCK_OR_GENERATION_INVALID')
        positive_int(self.min_tte_ns, 'TTE_INVALID', allow_zero=True)
        positive_int(self.max_tte_ns, 'TTE_INVALID')
        positive_int(self.matching_delay_ns, 'MATCHING_DELAY_INVALID', allow_zero=True)
        if (self.signal_monotonic_ns > self.clock.monotonic_ns
                or self.expires_monotonic_ns < self.clock.monotonic_ns
                or self.signal_expires_monotonic_ns < self.clock.monotonic_ns
                or self.min_tte_ns > self.max_tte_ns):
            raise ReplayError('INTENT_NONCAUSAL_OR_EXPIRED')

    @property
    def attempt_id(self) -> str:
        # Neither code SHA nor restart identity may reset deduplication.
        return digest((self.experiment_id, self.protocol_hash, self.market_id, self.signal_id, self.side))


@dataclass(frozen=True)
class LatencyScenario:
    profile_id: str
    processing_ns: int
    network_ns: int
    additional_ns: int
    depth_fraction: Decimal

    def __post_init__(self) -> None:
        nonempty(self.profile_id, 'LATENCY_PROFILE_MISSING')
        positive_int(self.processing_ns, 'PROCESSING_LATENCY_INVALID', allow_zero=True)
        positive_int(self.network_ns, 'POSITIVE_NETWORK_SCENARIO_REQUIRED')
        positive_int(self.additional_ns, 'ADDITIONAL_LATENCY_INVALID', allow_zero=True)
        if (not isinstance(self.depth_fraction, Decimal) or not self.depth_fraction.is_finite()
                or not 0 < self.depth_fraction <= 1):
            raise ReplayError('DEPTH_STRESS_INVALID')

    def arrival(self, intent: ReplayIntent) -> Clock:
        return intent.clock.after(self.processing_ns + self.network_ns
                                  + self.additional_ns + intent.matching_delay_ns)


@dataclass(frozen=True)
class FillResult:
    trace_id: str
    status: str
    reason: str
    arrival_clock: Clock
    snapshot_id: str | None
    requested: Decimal
    filled: Decimal
    net_shares: Decimal
    gross_cash: Decimal
    cash_fee: Decimal
    shares_fee: Decimal
    fee_value: Decimal
    levels: tuple[tuple[Decimal, Decimal], ...]
    evidence: str = 'SIMULATED'
    observed_exchange_execution_time: None = None
    exchange_ack: None = None

    @property
    def cash_delta(self) -> Decimal:
        return self.gross_cash - self.cash_fee


class DepthDepletion:
    """Replay-only NO_REPLENISHMENT_CREDIT debt shared by all simulated orders.

    Debt follows a price across later snapshots and epochs. This conservative
    public-L2 convention is not a model of counterfactual matching-engine impact.
    """
    def __init__(self) -> None:
        self._debt: dict[tuple[str, str, str, Decimal], Decimal] = {}

    def available(self, book: BookCut, side: str, price: Decimal, displayed: Decimal,
                  fraction: Decimal) -> Decimal:
        key = (book.market_id, book.token_id, side, price)
        return max(ZERO, displayed * fraction - self._debt.get(key, ZERO))

    def consume(self, book: BookCut, side: str, levels: tuple[tuple[Decimal, Decimal], ...]) -> None:
        for price, quantity in levels:
            key = (book.market_id, book.token_id, side, price)
            self._debt[key] = self._debt.get(key, ZERO) + quantity


def arrival_rejection(intent: ReplayIntent, cut: ArrivalCut, arrival: Clock) -> str | None:
    book, fee = cut.book, cut.fee
    if not intent.clock.same_domain(arrival) or not book.clock.same_domain(arrival):
        return 'CLOCK_DOMAIN_MISMATCH'
    if cut.available_monotonic_ns > arrival.monotonic_ns:
        return 'FUTURE_ARRIVAL_CUT'
    if cut.kill:
        return 'KILL'
    if cut.cutover_drain:
        return 'CUTOVER_DRAIN'
    if not cut.accounting_ready:
        return 'ACCOUNTING_MISMATCH'
    if arrival.monotonic_ns > intent.expires_monotonic_ns:
        return 'AUTHORIZATION_EXPIRED'
    if arrival.monotonic_ns > intent.signal_expires_monotonic_ns:
        return 'SIGNAL_EXPIRED'
    if not cut.accepting_orders or arrival.wall_ns >= cut.market_close_wall_ns:
        return 'MARKET_CLOSED'
    if not cut.rules_verified:
        return 'RULES_UNVERIFIED'
    if not cut.reference_ready:
        return 'MISSING_REFERENCE'
    if not cut.oracle_ready:
        return 'ORACLE_STALE'
    if not cut.required_feeds_ready:
        return 'EXTERNAL_FEED_GAP'
    if (book.market_id, book.token_id) != (intent.market_id, intent.token_id):
        return 'TOKEN_MAPPING_MISMATCH'
    if book.generation != intent.generation:
        return 'ROLLOVER_GENERATION_MISMATCH'
    if book.epoch != intent.epoch:
        return 'PM_BOOK_UNSYNCED'
    if book.rules_hash != intent.rules_hash:
        return 'RULES_CHANGED'
    if book.tick != intent.tick:
        return 'TICK_CHANGED'
    if not book.valid:
        return 'PM_BOOK_INVALID'
    if arrival.monotonic_ns - book.transport_monotonic_ns > intent.max_transport_age_ns:
        return 'PM_BOOK_TOO_OLD'
    if (not fee.verified or fee.market_id != intent.market_id
            or fee.observed_ns > arrival.monotonic_ns or fee.expires_ns < arrival.monotonic_ns):
        return 'FEES_UNKNOWN'
    if fee.version != intent.fee_version or book.fee_version != intent.fee_version:
        return 'FEE_VERSION_CHANGED'
    if cut.matching_delay_ns != intent.matching_delay_ns:
        return 'MATCHING_DELAY_CHANGED'
    if fee.incidence == 'BUY_SHARES' and intent.side != 'BUY':
        return 'FEE_INCIDENCE_UNSUPPORTED'
    if intent.quantity < book.min_size:
        return 'MINIMUM_SIZE'
    tte = cut.market_close_wall_ns - arrival.wall_ns
    if not intent.min_tte_ns <= tte <= intent.max_tte_ns:
        return 'TTE_OUTSIDE_WINDOW'
    return None


def simulate_arrival(intent: ReplayIntent, cut: ArrivalCut | None, scenario: LatencyScenario,
                     depletion: DepthDepletion) -> FillResult:
    """FAK reference: no HTTP, file polling, model fitting or authority issuance."""
    arrival = scenario.arrival(intent)
    def empty(status: str, reason: str) -> FillResult:
        return FillResult(intent.trace_id, status, reason, arrival,
                          cut.book.snapshot_id if cut else None, intent.quantity,
                          ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ())
    if cut is None:
        return empty('UNKNOWN', 'ARRIVAL_BOOK_UNOBSERVED')
    reason = arrival_rejection(intent, cut, arrival)
    if reason:
        unknown = reason in {'FUTURE_ARRIVAL_CUT', 'PM_BOOK_TOO_OLD', 'PM_BOOK_INVALID',
                             'PM_BOOK_UNSYNCED', 'FEES_UNKNOWN', 'CLOCK_DOMAIN_MISMATCH'}
        return empty('UNKNOWN' if unknown else 'REJECTED', reason)
    levels = cut.book.asks if intent.side == 'BUY' else cut.book.bids
    if not levels:
        return empty('UNFILLED', 'NO_FILL')
    remaining, gross, fee_cash, fee_shares, fee_value = intent.quantity, ZERO, ZERO, ZERO, ZERO
    matched: list[tuple[Decimal, Decimal]] = []
    for price, displayed in levels:
        if (intent.side == 'BUY' and price > intent.limit_price) or (intent.side == 'SELL' and price < intent.limit_price):
            break
        quantity = min(remaining, depletion.available(cut.book, intent.side, price, displayed, scenario.depth_fraction))
        if quantity <= 0:
            continue
        charge = cut.fee.charge(price, quantity)
        if cut.fee.incidence == 'BUY_SHARES':
            shares_charge = cut.fee.shares_charge(charge, price)
            if shares_charge > quantity:
                return empty('UNKNOWN', 'FEE_EXCEEDS_FILL')
            fee_shares += shares_charge
        else:
            fee_cash += charge
        gross += price * quantity
        fee_value += charge
        matched.append((price, quantity))
        remaining -= quantity
        if remaining == 0:
            break
    filled = intent.quantity - remaining
    if filled == 0:
        worse = ((intent.side == 'BUY' and levels[0][0] > intent.limit_price)
                 or (intent.side == 'SELL' and levels[0][0] < intent.limit_price))
        return empty('UNFILLED', 'ARRIVAL_PRICE_WORSE' if worse else 'NO_FILL')
    matched_tuple = tuple(matched)
    depletion.consume(cut.book, intent.side, matched_tuple)
    return FillResult(intent.trace_id, 'FILLED' if remaining == 0 else 'PARTIAL',
                      'FULL_FILL' if remaining == 0 else 'PARTIAL_FILL', arrival,
                      cut.book.snapshot_id, intent.quantity, filled, filled - fee_shares,
                      -gross if intent.side == 'BUY' else gross, fee_cash, fee_shares, fee_value, matched_tuple)


class ArrivalTape:
    """Causal as-of lookup. Invalid latest state never falls back to older good state."""
    def __init__(self, cuts: Iterable[ArrivalCut]):
        self._cuts: dict[tuple[str, str, str, str], list[ArrivalCut]] = {}
        identities: dict[str, str] = {}
        for cut in cuts:
            key = (cut.book.market_id, cut.book.token_id, cut.book.clock.host_id, cut.book.clock.boot_id)
            book_key = digest((key, cut.book.snapshot_id))
            payload = primitive(cut.book)
            # A transport heartbeat may advance without a price/depth change.
            payload.pop('transport_monotonic_ns')
            book_hash = digest(payload)
            if book_key in identities and identities[book_key] != book_hash:
                raise ReplayError('SNAPSHOT_ID_REDEFINED')
            identities[book_key] = book_hash
            self._cuts.setdefault(key, []).append(cut)
        self._times: dict[tuple[str, str, str, str], list[int]] = {}
        for key, values in self._cuts.items():
            values.sort(key=lambda x: x.available_monotonic_ns)
            for previous, current in zip(values, values[1:]):
                if previous.available_monotonic_ns == current.available_monotonic_ns and previous != current:
                    raise ReplayError('AMBIGUOUS_CUT_ORDER')
            self._times[key] = [x.available_monotonic_ns for x in values]

    def at(self, intent: ReplayIntent, clock: Clock) -> ArrivalCut | None:
        key = (intent.market_id, intent.token_id, clock.host_id, clock.boot_id)
        index = bisect_right(self._times.get(key, []), clock.monotonic_ns) - 1
        return self._cuts[key][index] if index >= 0 else None


@dataclass(frozen=True)
class ResolutionProof:
    market_id: str
    token_payouts: tuple[tuple[str, Decimal], ...]
    source: str
    source_record_hash: str
    available_wall_ns: int
    status: str

    def __post_init__(self) -> None:
        nonempty(self.market_id, 'RESOLUTION_MARKET_MISSING')
        positive_int(self.available_wall_ns, 'RESOLUTION_CLOCK_INVALID')
        if not isinstance(self.token_payouts, tuple):
            raise ReplayError('MUTABLE_RESOLUTION')
        if self.status not in ('RESOLVED', 'UNRESOLVED', 'DISPUTED'):
            raise ReplayError('RESOLUTION_STATUS_INVALID')
        if self.status == 'RESOLVED':
            if self.source not in ('OFFICIAL_GAMMA_RESOLUTION', 'OFFICIAL_CTF_PAYOUT'):
                raise ReplayError('RESOLUTION_SOURCE_UNVERIFIED')
            if len(self.source_record_hash) != 64 or any(c not in '0123456789abcdef' for c in self.source_record_hash):
                raise ReplayError('RESOLUTION_PROVENANCE_MISSING')
            if len(self.token_payouts) != 2 or len({t for t, _ in self.token_payouts}) != 2:
                raise ReplayError('RESOLUTION_TOKEN_MAPPING_INVALID')
            if any(not isinstance(t, str) or not t or not isinstance(p, Decimal)
                   or not p.is_finite() or not 0 <= p <= 1 for t, p in self.token_payouts):
                raise ReplayError('RESOLUTION_PAYOUT_INVALID')
            if sum((p for _, p in self.token_payouts), ZERO) != 1:
                raise ReplayError('RESOLUTION_PAYOUT_SUM_INVALID')
        elif self.token_payouts:
            raise ReplayError('UNRESOLVED_MUST_NOT_HAVE_PAYOUT')

    @classmethod
    def from_gamma(cls, raw: Mapping[str, Any], *, expected_market: str,
                   expected_tokens: tuple[str, str], available_wall_ns: int) -> 'ResolutionProof':
        """Validate a supplied official API record, without claiming signature verification."""
        if str(raw.get('id', '')) != expected_market:
            raise ReplayError('RESOLUTION_MARKET_MISMATCH')
        if len(expected_tokens) != 2 or len(set(expected_tokens)) != 2:
            raise ReplayError('RESOLUTION_TOKEN_MAPPING_INVALID')
        status = str(raw.get('umaResolutionStatus', '')).lower()
        if status != 'resolved' or raw.get('closed') is not True:
            state = 'DISPUTED' if status in ('disputed', 'challenged') else 'UNRESOLVED'
            return cls(expected_market, (), 'OFFICIAL_GAMMA_RESOLUTION', digest(raw), available_wall_ns, state)
        def array(value: Any) -> list[Any]:
            result = json.loads(value) if isinstance(value, str) else value
            if not isinstance(result, list):
                raise ReplayError('RESOLUTION_ARRAY_INVALID')
            return result
        tokens, prices = array(raw.get('clobTokenIds')), array(raw.get('outcomePrices'))
        if len(tokens) != 2 or len(prices) != 2 or set(tokens) != set(expected_tokens):
            raise ReplayError('RESOLUTION_TOKEN_MAPPING_INVALID')
        payouts = tuple((t, decimal(p)) for t, p in zip(tokens, prices))
        if sorted(p for _, p in payouts) != [ZERO, D(1)]:
            return cls(expected_market, (), 'OFFICIAL_GAMMA_RESOLUTION', digest(raw), available_wall_ns, 'UNRESOLVED')
        return cls(expected_market, payouts, 'OFFICIAL_GAMMA_RESOLUTION', digest(raw), available_wall_ns, 'RESOLVED')


def settlement_pnl(intent: ReplayIntent, result: FillResult, proof: ResolutionProof | None,
                   *, as_of_wall_ns: int) -> Decimal | None:
    if result.status == 'UNKNOWN':
        return None
    if result.filled == 0:
        return ZERO  # Known nonfill, not a missing settlement.
    if intent.side != 'BUY':
        raise ReplayError('SETTLEMENT_PNL_REQUIRES_LONG_ENTRY')
    if (proof is None or proof.status != 'RESOLVED' or proof.market_id != intent.market_id
            or proof.available_wall_ns > as_of_wall_ns):
        return None
    if proof.available_wall_ns < result.arrival_clock.wall_ns:
        raise ReplayError('RESOLUTION_PRECEDES_ENTRY')
    payouts = dict(proof.token_payouts)
    if intent.token_id not in payouts:
        raise ReplayError('RESOLUTION_TOKEN_MISMATCH')
    return result.net_shares * payouts[intent.token_id] + result.cash_delta
