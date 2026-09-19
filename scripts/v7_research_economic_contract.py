"""Immutable executable-label contracts. Offline research; no order authority.

The result is a visible-book counterfactual, never an observed exchange fill.
Fee inputs and mandatory delays must come from a versioned market observation.
No category-wide default, midpoint fill, future interpolation or price chase.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from functools import wraps
from decimal import Decimal, localcontext
import hashlib
import json
from typing import Any

SCHEMA = "polymarket_v7_executable_research_label_v1"
AUTHORITY = "ZERO_AUTHORITY_RESEARCH_ONLY"
D = Decimal


class ResearchContractError(ValueError):
    """A missing or inconsistent contract is not a zero-valued observation."""


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ResearchContractError("invalid_decimal")
    try:
        result = D(str(value))
    except Exception as exc:
        raise ResearchContractError("invalid_decimal") from exc
    if not result.is_finite() or abs(result) > D("1e18") or len(result.as_tuple().digits) > 18 or abs(result.as_tuple().exponent) > 18:
        raise ResearchContractError("invalid_decimal")
    return result


def fixed_decimal_context(function):
    @wraps(function)
    def run(*args, **kwargs):
        with localcontext() as context:
            context.prec = 128
            return function(*args, **kwargs)
    return run


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResearchContractError(name)
    return value


def identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ResearchContractError(name)
    return value


def digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ResearchContractError(name)
    return value


def canonical_hash(value: Any) -> str:
    def encode(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj, "f")
        raise TypeError(type(obj).__name__)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class MarketEconomics:
    market_id: str
    yes_token: str
    no_token: str
    clock_domain: str
    available_ns: int
    valid_until_ns: int
    settlement_semantic_hash: str
    rules_hash: str
    source_hash: str
    tick_size: Decimal
    minimum_shares: Decimal
    taker_fee_rate: Decimal
    fee_formula: str
    delay_enabled: bool
    mandatory_delay_ns: int

    def __post_init__(self) -> None:
        for name in ("market_id", "yes_token", "no_token", "clock_domain"):
            identity(getattr(self, name), name)
        if self.yes_token == self.no_token:
            raise ResearchContractError("outcome_mapping")
        for name in ("settlement_semantic_hash", "rules_hash", "source_hash"):
            digest(getattr(self, name), name)
        integer(self.available_ns, "metadata_available_ns", 1)
        integer(self.valid_until_ns, "metadata_valid_until_ns", self.available_ns + 1)
        integer(self.mandatory_delay_ns, "mandatory_delay_ns")
        if type(self.delay_enabled) is not bool or self.delay_enabled != (self.mandatory_delay_ns > 0):
            raise ResearchContractError("delay_metadata_inconsistent")
        for name in ("tick_size", "minimum_shares", "taker_fee_rate"):
            object.__setattr__(self, name, decimal(getattr(self, name)))
        if not 0 < self.tick_size < 1 or self.minimum_shares <= 0 or not 0 <= self.taker_fee_rate <= 1:
            raise ResearchContractError("market_economics_range")
        if self.fee_formula != "C_RATE_P_ONE_MINUS_P_V1":
            raise ResearchContractError("unsupported_fee_formula")

    @property
    def hash(self) -> str:
        return canonical_hash(asdict(self))

    def check_time(self, timestamp_ns: int) -> None:
        integer(timestamp_ns, "economic_timestamp", 1)
        if not self.available_ns <= timestamp_ns < self.valid_until_ns:
            raise ResearchContractError("metadata_not_available_or_expired")

    @fixed_decimal_context
    def check_price(self, price: Decimal) -> None:
        if not 0 < price < 1 or price % self.tick_size != 0:
            raise ResearchContractError("invalid_price_tick")

    def fee(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Unrounded price-level estimate, not exchange fee-rounding parity."""
        quantity, price = decimal(quantity), decimal(price)
        if quantity < 0:
            raise ResearchContractError("negative_fill_quantity")
        self.check_price(price)
        with localcontext() as context:
            context.prec = 128
            return quantity * self.taker_fee_rate * price * (1 - price)


@dataclass(frozen=True)
class LatencyScenario:
    duration_ns: int
    scope: str
    source_hash: str
    available_ns: int
    clock_domain: str

    def __post_init__(self) -> None:
        integer(self.duration_ns, "latency_duration")
        integer(self.available_ns, "latency_available", 1)
        identity(self.clock_domain, "latency_clock_domain")
        digest(self.source_hash, "latency_source")
        if self.scope not in {"EXCLUDES_MARKET_DELAY", "INCLUDES_MARKET_DELAY"}:
            raise ResearchContractError("latency_scope_unknown")

    def arrival(self, decision_ns: int, market: MarketEconomics) -> int:
        if self.clock_domain != market.clock_domain or self.available_ns > decision_ns:
            raise ResearchContractError("latency_not_causal")
        market.check_time(decision_ns)
        if self.scope == "INCLUDES_MARKET_DELAY":
            if self.duration_ns < market.mandatory_delay_ns:
                raise ResearchContractError("latency_smaller_than_included_delay")
            delay = self.duration_ns
        else:
            delay = self.duration_ns + market.mandatory_delay_ns
        arrival = decision_ns + delay
        market.check_time(arrival)
        return arrival


@dataclass(frozen=True)
class BookFrame:
    market_id: str
    token_id: str
    clock_domain: str
    session_id: str
    connection_epoch: int
    sequence: int
    receive_ns: int
    applied_ns: int
    available_ns: int
    economics_hash: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    def __post_init__(self) -> None:
        for name in ("market_id", "token_id", "clock_domain", "session_id"):
            identity(getattr(self, name), name)
        for name in ("connection_epoch", "sequence", "receive_ns", "applied_ns", "available_ns"):
            integer(getattr(self, name), name, 1)
        if not self.receive_ns <= self.applied_ns <= self.available_ns:
            raise ResearchContractError("book_availability_order")
        digest(self.economics_hash, "book_economics_hash")
        for name in ("bids", "asks"):
            raw = getattr(self, name)
            if not isinstance(raw, (tuple, list)) or len(raw) > 10000:
                raise ResearchContractError("book_levels_shape")
            try:
                levels = tuple((decimal(price), decimal(size)) for price, size in raw)
            except (TypeError, ValueError) as exc:
                raise ResearchContractError("book_levels_shape") from exc
            if any(not 0 < p < 1 or q <= 0 for p, q in levels):
                raise ResearchContractError("book_level_range")
            prices = [p for p, _ in levels]
            if len(set(prices)) != len(prices) or prices != sorted(prices, reverse=name == "bids"):
                raise ResearchContractError("book_level_order")
            object.__setattr__(self, name, levels)
        if self.bids and self.asks and self.bids[0][0] >= self.asks[0][0]:
            raise ResearchContractError("crossed_book")

    @property
    def hash(self) -> str:
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class CoverageProof:
    """Certification supplied by a recorder; never inferred from a quiet book.

    A producer must invalidate this proof on any drop, unresolved gap or restart.
    proof_available_ns belongs to the label side, not the prediction feature cut.
    """
    clock_domain: str
    session_id: str
    connection_epoch: int
    start_ns: int
    end_ns: int
    proof_available_ns: int
    source_hash: str
    complete: bool

    def __post_init__(self) -> None:
        identity(self.clock_domain, "coverage_clock_domain")
        identity(self.session_id, "coverage_session")
        integer(self.connection_epoch, "coverage_epoch", 1)
        integer(self.start_ns, "coverage_start", 1)
        integer(self.end_ns, "coverage_end", self.start_ns)
        integer(self.proof_available_ns, "proof_available", self.end_ns)
        digest(self.source_hash, "coverage_hash")
        if self.complete is not True:
            raise ResearchContractError("incomplete_coverage")


class BookSeries:
    """Indexed as-of lookup over one certified stream, using availability time."""
    def __init__(self, frames: tuple[BookFrame, ...], proof: CoverageProof):
        if not frames:
            raise ResearchContractError("missing_book_history")
        self.frames, self.proof = tuple(frames), proof
        first = self.frames[0]
        previous_sequence, previous_available = 0, 0
        for frame in self.frames:
            if (frame.market_id, frame.token_id, frame.clock_domain, frame.session_id, frame.connection_epoch) != (
                first.market_id, first.token_id, proof.clock_domain, proof.session_id, proof.connection_epoch
            ):
                raise ResearchContractError("mixed_book_lineage")
            if frame.sequence <= previous_sequence or frame.available_ns < previous_available:
                raise ResearchContractError("book_application_order")
            if not proof.start_ns <= frame.available_ns <= proof.end_ns:
                raise ResearchContractError("book_outside_coverage")
            previous_sequence, previous_available = frame.sequence, frame.available_ns
        self.times = tuple(frame.available_ns for frame in self.frames)

    def asof(self, target_ns: int, economics_hash: str) -> BookFrame:
        integer(target_ns, "asof_target", 1)
        if not self.proof.start_ns <= target_ns <= self.proof.end_ns:
            raise ResearchContractError("target_not_covered")
        index = bisect_right(self.times, target_ns) - 1
        if index < 0:
            raise ResearchContractError("no_asof_book")
        frame = self.frames[index]
        if frame.economics_hash != economics_hash:
            raise ResearchContractError("market_economics_changed")
        return frame


@dataclass(frozen=True)
class ResearchDecision:
    decision_id: str
    market_id: str
    token_id: str
    clock_domain: str
    decision_ns: int
    feature_available_ns: int
    feature_hash: str
    policy_hash: str
    economics_hash: str
    origin_book_hash: str
    quantity: Decimal
    limit_price: Decimal
    time_in_force: str

    def __post_init__(self) -> None:
        for name in ("decision_id", "market_id", "token_id", "clock_domain"):
            identity(getattr(self, name), name)
        integer(self.decision_ns, "decision_ns", 1)
        integer(self.feature_available_ns, "feature_available_ns", 1)
        if self.feature_available_ns > self.decision_ns:
            raise ResearchContractError("future_feature")
        for name in ("feature_hash", "policy_hash", "economics_hash", "origin_book_hash"):
            digest(getattr(self, name), name)
        object.__setattr__(self, "quantity", decimal(self.quantity))
        object.__setattr__(self, "limit_price", decimal(self.limit_price))
        if self.quantity <= 0 or not 0 < self.limit_price < 1 or self.time_in_force not in {"FAK", "FOK"}:
            raise ResearchContractError("decision_order_contract")


@dataclass(frozen=True)
class Sweep:
    quantity: Decimal
    notional: Decimal
    fees: Decimal
    requested_quantity: Decimal
    side: str
    fills: tuple[tuple[Decimal, Decimal], ...]


@fixed_decimal_context
def sweep(book: BookFrame, market: MarketEconomics, *, side: str,
          quantity: Decimal, limit_price: Decimal, time_in_force: str) -> Sweep:
    quantity, limit_price = decimal(quantity), decimal(limit_price)
    market.check_price(limit_price)
    if side not in {"BUY", "SELL"} or time_in_force not in {"FAK", "FOK"}:
        raise ResearchContractError("sweep_order_contract")
    if quantity < market.minimum_shares:
        raise ResearchContractError("below_minimum_shares")
    if book.clock_domain != market.clock_domain or book.market_id != market.market_id or book.token_id not in {market.yes_token, market.no_token} or book.economics_hash != market.hash:
        raise ResearchContractError("sweep_context_mismatch")
    remaining, notional, fees = quantity, D(0), D(0)
    fills = []
    for price, displayed in (book.asks if side == "BUY" else book.bids):
        market.check_price(price)
        if (side == "BUY" and price > limit_price) or (side == "SELL" and price < limit_price):
            break
        size = min(remaining, displayed)
        fills.append((price, size))
        notional += price * size
        fees += market.fee(size, price)
        remaining -= size
        if remaining == 0:
            break
    if time_in_force == "FOK" and remaining > 0:
        return Sweep(D(0), D(0), D(0), quantity, side, ())
    return Sweep(quantity - remaining, notional, fees, quantity, side, tuple(fills))


@fixed_decimal_context
def round_trip_label(decision: ResearchDecision, market: MarketEconomics, books: BookSeries,
                     entry_latency: LatencyScenario, exit_latency: LatencyScenario,
                     holding_ns: int) -> dict[str, Any]:
    """Buy under a frozen cap; sell at the bid known at the exit decision.

    Both legs incur their own latency and mandatory delay. Unliquidated inventory
    is censored, never assigned a zero or a realized terminal PnL. Snapshots are
    exogenous: no queue, market impact or matching-priority claim is made.
    """
    integer(holding_ns, "holding_ns", 1)
    if (decision.market_id, decision.clock_domain, decision.economics_hash) != (
        market.market_id, market.clock_domain, market.hash
    ) or decision.token_id not in {market.yes_token, market.no_token}:
        raise ResearchContractError("decision_context_mismatch")
    if books.proof.clock_domain != market.clock_domain:
        raise ResearchContractError("mixed_clock_domains")
    if books.frames[0].token_id != decision.token_id:
        raise ResearchContractError("decision_token_mismatch")
    origin = books.asof(decision.decision_ns, market.hash)
    if origin.hash != decision.origin_book_hash:
        raise ResearchContractError("origin_book_not_decision_cut")
    # Freeze both latency scenarios at entry; future measurements cannot enter.
    for latency in (entry_latency, exit_latency):
        if latency.available_ns > decision.decision_ns:
            raise ResearchContractError("future_latency_scenario")
    entry_ns = entry_latency.arrival(decision.decision_ns, market)
    entry_book = books.asof(entry_ns, market.hash)
    buy = sweep(entry_book, market, side="BUY", quantity=decision.quantity,
                limit_price=decision.limit_price, time_in_force=decision.time_in_force)
    result = {
        "schema": SCHEMA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "execution_authority": AUTHORITY, "research_only": True,
        "evidence_kind": "VISIBLE_BOOK_COUNTERFACTUAL_NOT_OBSERVED_FILL",
        "fee_semantics": "UNROUNDED_PRICE_LEVEL_ESTIMATE",
        "assumptions": ["EXOGENOUS_BOOK_NO_SELF_IMPACT", "NO_QUEUE_PRIORITY_CLAIM",
                        "NO_MATCHING_GUARANTEE", "NO_REBATES_ASSUMED"],
        "decision_id": decision.decision_id, "market_id": decision.market_id,
        "token_id": decision.token_id, "clock_domain": decision.clock_domain,
        "decision_hash": canonical_hash(asdict(decision)), "economics_hash": market.hash,
        "policy_hash": decision.policy_hash, "feature_hash": decision.feature_hash,
        "origin_ns": decision.decision_ns, "feature_available_ns": decision.feature_available_ns,
        "entry_ns": entry_ns, "entry_book_hash": entry_book.hash,
        "entry_quantity": str(buy.quantity), "entry_notional": str(buy.notional),
        "entry_fees": str(buy.fees), "holding_ns": holding_ns,
        "latency_hash": canonical_hash([asdict(entry_latency), asdict(exit_latency)]),
        "coverage_hash": books.proof.source_hash,
        "label_available_ns": books.proof.proof_available_ns,
    }
    if buy.quantity == 0:
        result.update(status="NO_FILL", exit_ns=None, information_end_ns=entry_ns,
                      remaining_quantity="0", cash_delta="0", net_pnl="0")
    else:
        exit_decision_ns = entry_ns + holding_ns
        exit_ns = exit_latency.arrival(exit_decision_ns, market)
        exit_cut = books.asof(exit_decision_ns, market.hash)
        exit_book = books.asof(exit_ns, market.hash)
        if not exit_cut.bids or buy.quantity < market.minimum_shares:
            sell = Sweep(D(0), D(0), D(0), buy.quantity, "SELL", ())
        else:
            sell = sweep(exit_book, market, side="SELL", quantity=buy.quantity,
                         limit_price=exit_cut.bids[0][0], time_in_force="FAK")
        remaining = buy.quantity - sell.quantity
        cash = sell.notional - sell.fees - buy.notional - buy.fees
        result.update(status="COMPLETE" if remaining == 0 else "OPEN_RESIDUAL",
                      exit_decision_ns=exit_decision_ns, exit_ns=exit_ns,
                      information_end_ns=exit_ns, exit_book_hash=exit_book.hash,
                      exit_decision_book_hash=exit_cut.hash, exit_quantity=str(sell.quantity),
                      exit_notional=str(sell.notional), exit_fees=str(sell.fees),
                      remaining_quantity=str(remaining), cash_delta=str(cash),
                      net_pnl=str(cash) if remaining == 0 else None)
    for frame in books.frames:
        if decision.decision_ns <= frame.available_ns <= result["information_end_ns"] and frame.economics_hash != market.hash:
            raise ResearchContractError("market_economics_changed_during_label")
    result["label_hash"] = canonical_hash(result)
    return result
