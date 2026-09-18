"""Deterministic, zero-authority research execution on native consumed-state tapes.

Integer prices are E4 and quantities microunits. No order submission functions.
Missing/gapped arrival data is CENSORED, not an observed nonfill or zero PnL.
This is a local-receive-time replay, NOT a claim to know exchange queue state.
"""
from __future__ import annotations
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from typing import Any, Iterable


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class Book:
    market: str
    token: str
    capture: str
    ts_ns: int
    version: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
    tick: int
    valid: bool = True
    receive_ns: int | None = None

    def __post_init__(self):
        if not self.market or not self.token or not self.capture or self.ts_ns <= 0 or self.tick <= 0:
            raise EvidenceError('book identity/time/tick')
        for side, reverse in ((self.bids, True), (self.asks, False)):
            prices = [x[0] for x in side]
            if prices != sorted(set(prices), reverse=reverse):
                raise EvidenceError('unsorted/duplicate depth')
            if any(not 0 < p < 10000 or p % self.tick or q < 0 for p, q in side):
                raise EvidenceError('invalid price/quantity')
        if self.valid and (not self.bids or not self.asks or self.bids[0][0] >= self.asks[0][0]):
            raise EvidenceError('crossed/incomplete valid book')


@dataclass(frozen=True)
class Order:
    order_id: str
    market: str
    token: str
    capture: str
    decision_ns: int
    side: str
    limit_e4: int
    quantity: int
    minimum: int
    delay_ns: int = 0
    full_depth_required: bool = True


@dataclass
class Execution:
    status: str
    arrival_ns: int
    filled_quantity: int = 0
    notional: float = 0.0
    fee: float = 0.0
    average_price: float | None = None
    reason: str = ''
    book_version: int | None = None


@dataclass
class BookTape:
    books: dict[tuple[str, str, str], list[Book]] = field(default_factory=dict)
    times: dict[tuple[str, str, str], list[int]] = field(default_factory=dict)
    watermarks: dict[str, int] = field(default_factory=dict)
    gaps: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def from_books(cls, books: Iterable[Book], watermarks: dict[str, int],
                   gaps: dict[str, list[tuple[int, int]]] | None = None) -> 'BookTape':
        out = cls(watermarks=dict(watermarks), gaps=gaps or {})
        for book in books:
            key = book.capture, book.market, book.token
            seq = out.books.setdefault(key, [])
            if seq and book.ts_ns < seq[-1].ts_ns:
                raise EvidenceError('receive clock moved backwards')
            if seq and book.ts_ns == seq[-1].ts_ns and book.version == seq[-1].version:
                if book != seq[-1]:
                    raise EvidenceError('same version different book')
                continue
            seq.append(book)
        out.times = {key:[book.ts_ns for book in seq] for key,seq in out.books.items()}
        return out

    def at(self, capture: str, market: str, token: str, ts: int,
           max_age_ns: int, since_ns: int | None = None) -> Book:
        if self.watermarks.get(capture, -1) < ts:
            raise EvidenceError('arrival_after_capture_watermark')
        for start, end in self.gaps.get(capture, []):
            if start <= ts and end >= (since_ns if since_ns is not None else ts):
                raise EvidenceError('capture_gap')
        key = capture, market, token
        idx = bisect_right(self.times.get(key, []), ts) - 1
        if idx < 0:
            raise EvidenceError('arrival_book_missing')
        book = self.books[key][idx]
        if not book.valid:
            raise EvidenceError('arrival_book_invalid')
        if ts - (book.receive_ns if book.receive_ns is not None else book.ts_ns) > max_age_ns:
            raise EvidenceError('arrival_book_stale')
        return book


def cash_fee(quantity: int, price_e4: int, rate: float, exponent: float,
             cost_multiplier: float = 1.0) -> float:
    """Explicit research cash-fee convention; live fee-asset parity is a gate."""
    if not all(isfinite(x) for x in (rate, exponent, cost_multiplier)) or min(rate, exponent, cost_multiplier) < 0:
        raise EvidenceError('invalid fee schedule')
    price = price_e4 / 10000
    amount = quantity / 1_000_000 * rate * (price * (1-price)) ** exponent * cost_multiplier
    return float(Decimal(str(amount)).quantize(Decimal('.00001'), rounding=ROUND_HALF_UP))


class CausalReplay:
    """One independent replay scenario with shared consumption and inventory.

    Consumed public liquidity is not replenished just because a new message has
    a new version. Only additional visible depth at a price adds new capacity.
    This is intentionally conservative; report sensitivity, not exact FIFO.
    """
    def __init__(self, tape: BookTape, *, rate: float, exponent: float = 1.0,
                 max_book_age_ns: int = 100_000_000, cost_multiplier: float = 1.0,
                 capital_budget: float = 1000.0, max_order_cost: float = 10.0,
                 max_market_cost: float = 100.0):
        self.tape, self.rate, self.exponent = tape, rate, exponent
        self.max_book_age_ns, self.cost_multiplier = max_book_age_ns, cost_multiplier
        self.inventory: dict[tuple[str, str], int] = defaultdict(int)
        self.cash = 0.0
        if not all(isfinite(x) and x>0 for x in (capital_budget,max_order_cost,max_market_cost)):
            raise EvidenceError('invalid capital limits')
        self.capital_budget,self.max_order_cost,self.max_market_cost=capital_budget,max_order_cost,max_market_cost
        self.inventory_cost: dict[tuple[str,str],float] = defaultdict(float)
        self._depth: dict[tuple[str,str,str,str,int], list[int]] = {}
        self._last_book: dict[tuple[str,str,str,str], int] = {}
        self._orders: set[str] = set()
        self._last_arrival = 0

    def _available(self, book: Book, side: str) -> list[tuple[int,int]]:
        key = book.capture, book.market, book.token, side
        levels = book.asks if side == 'BUY' else book.bids
        seen = set()
        out = []
        for price, visible in levels:
            pk = (*key, price)
            previous, remaining = self._depth.get(pk, [0,0])
            if self._last_book.get(key) != book.ts_ns:
                remaining = min(visible, remaining + max(0, visible - previous))
                self._depth[pk] = [visible, remaining]
            else:
                remaining = self._depth[pk][1]
            out.append((price, remaining)); seen.add(pk)
        for pk in list(self._depth):
            if pk[:4] == key and pk not in seen:
                self._depth[pk] = [0,0]
        self._last_book[key] = book.ts_ns
        return out

    def execute(self, order: Order) -> Execution:
        if order.order_id in self._orders:
            raise EvidenceError('duplicate_order_identity')
        if (order.side not in {'BUY','SELL'} or order.decision_ns <= 0 or order.delay_ns < 0
                or order.quantity <= 0 or order.minimum <= 0 or not 0 < order.limit_e4 < 10000):
            raise EvidenceError('invalid order')
        arrival = order.decision_ns + order.delay_ns
        if arrival < self._last_arrival:
            raise EvidenceError('orders must be processed in arrival order')
        self._last_arrival = arrival
        self._orders.add(order.order_id)
        if order.quantity < order.minimum:
            return Execution('REJECTED', arrival, reason='venue_minimum')
        if order.side == 'SELL' and self.inventory[order.market,order.token] < order.quantity:
            return Execution('REJECTED', arrival, reason='inventory_unavailable')
        if order.side == 'BUY':
            maximum_fee=order.quantity/1_000_000*self.rate*(.25**self.exponent)*self.cost_multiplier
            maximum_debit=order.quantity/1_000_000*order.limit_e4/10000+maximum_fee
            market_cost=sum(v for (m,_),v in self.inventory_cost.items() if m==order.market)
            capacity=min(self.max_order_cost,self.max_market_cost-market_cost,
                self.capital_budget-sum(self.inventory_cost.values()),self.capital_budget+self.cash)
            if maximum_debit>capacity+1e-9:
                return Execution('REJECTED',arrival,reason='capital_or_notional_cap')
        try:
            book = self.tape.at(order.capture, order.market, order.token, arrival,
                self.max_book_age_ns, since_ns=order.decision_ns)
        except EvidenceError as exc:
            return Execution('CENSORED', arrival, reason=str(exc))
        if order.limit_e4 % book.tick:
            return Execution('REJECTED', arrival, reason='invalid_tick', book_version=book.version)
        levels = [(p,q) for p,q in self._available(book,order.side)
            if (p <= order.limit_e4 if order.side == 'BUY' else p >= order.limit_e4)]
        if order.full_depth_required and sum(q for _,q in levels) < order.quantity:
            return Execution('NONFILL', arrival, reason='insufficient_visible_depth', book_version=book.version)
        remaining, notional, fee = order.quantity, 0.0, 0.0
        for price, depth in levels:
            amount = min(remaining, depth)
            if amount:
                notional += amount / 1_000_000 * price / 10000
                fee += cash_fee(amount,price,self.rate,self.exponent,self.cost_multiplier)
                self._depth[book.capture,book.market,book.token,order.side,price][1] -= amount
                remaining -= amount
            if remaining == 0: break
        filled = order.quantity - remaining
        if not filled:
            return Execution('NONFILL',arrival,reason='not_marketable',book_version=book.version)
        sign = 1 if order.side == 'BUY' else -1
        position=(order.market,order.token)
        previous_quantity=self.inventory[position]
        if sign>0:self.inventory_cost[position]+=notional+fee
        else:self.inventory_cost[position]*=1-filled/previous_quantity
        self.inventory[position] += sign * filled
        self.cash += -sign * notional - fee
        return Execution('FILLED' if remaining == 0 else 'PARTIAL',arrival,filled,notional,fee,
            notional/(filled/1_000_000),book_version=book.version)

    def settlement(self, market: str, winner: str) -> float:
        if not winner:
            raise EvidenceError('unknown winner cannot be zero')
        payout = self.inventory[market,winner] / 1_000_000
        self.cash += payout
        for key in list(self.inventory):
            if key[0] == market:
                self.inventory[key] = 0
                self.inventory_cost[key] = 0.0
        return payout


@dataclass
class RestingOrder:
    """Conservative fixed-price maker queue; cancellations do not imply fills."""
    order_id: str
    market: str
    token: str
    side: str
    price_e4: int
    quantity: int
    ahead: int
    arrival_ns: int
    cancel_effective_ns: int | None = None
    remaining: int | None = None

    def __post_init__(self):
        if self.side not in {'BUY','SELL'} or self.quantity <= 0 or self.ahead < 0 or self.arrival_ns <= 0:
            raise EvidenceError('maker order invalid')
        if self.remaining is None: self.remaining = self.quantity


def allocate_print(orders: list[RestingOrder], *, market: str, token: str,
                   ts_ns: int, aggressor: str, price_e4: int, quantity: int) -> dict[str,int]:
    """Conserve one public print across counterfactual orders in a scenario.

    One scenario must use a single aggregate queue-ahead estimate per price.
    Remaining conservative queue for later own orders must not double-count the
    same external queue; chronological sorting establishes own priority.
    """
    if quantity < 0 or aggressor not in {'BUY','SELL'}: raise EvidenceError('invalid trade')
    candidates = sorted([o for o in orders if o.market == market and o.token == token
        and o.side != aggressor and o.price_e4 == price_e4
        and o.arrival_ns < ts_ns and (o.cancel_effective_ns is None or ts_ns < o.cancel_effective_ns)
        and o.remaining], key=lambda o:(o.arrival_ns,o.order_id))
    fills: dict[str,int] = {}
    budget = quantity
    for o in candidates:
        removed = min(budget,o.ahead); o.ahead -= removed; budget -= removed
        fill = min(budget,int(o.remaining or 0)); o.remaining = int(o.remaining or 0)-fill; budget -= fill
        if fill: fills[o.order_id]=fill
        if not budget: break
    return fills
