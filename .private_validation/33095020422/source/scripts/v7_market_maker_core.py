#!/usr/bin/env python3
"""Professional V7 PAPER market-maker decision core.

The maker is a microstructure/control strategy, not a directional-alpha sleeve.
It chooses post-only quote actions by fill-conditioned trading economics plus
separately accounted maker rebates and liquidity rewards.  Cold-start PAPER
exploration is intentionally supported so the system can learn fills/markouts
without pretending that exploratory quotes are promotion-grade alpha.

This module is pure/deterministic and has no network or authenticated trading
surface.  The low-latency runtime may call the same formulas from a C++ fast
path; canonical evidence still flows through the single V7 ledger writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any, Iterable

EPS = 1e-12


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


@dataclass(frozen=True)
class BookState:
    token_id: str
    bid: float
    ask: float
    bid_depth: float
    ask_depth: float
    tick_size: float = 0.01
    microprice: float | None = None
    ofi: float = 0.0
    short_volatility: float = 0.0
    queue_bid: float = 0.0
    queue_ask: float = 0.0
    exchange_ts_ms: int = 0
    receive_ts_ms: int = 0
    snapshot_id: str = ""

    def validate(self) -> None:
        if not self.token_id:
            raise ValueError("book:missing_token")
        if not (0.0 < self.bid < self.ask < 1.0):
            raise ValueError("book:invalid_bid_ask")
        if self.bid_depth < 0.0 or self.ask_depth < 0.0:
            raise ValueError("book:negative_depth")
        if self.tick_size <= 0.0:
            raise ValueError("book:invalid_tick")

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def imbalance(self) -> float:
        return (self.bid_depth - self.ask_depth) / (self.bid_depth + self.ask_depth + EPS)

    @property
    def micro(self) -> float:
        if self.microprice is not None and math.isfinite(self.microprice):
            return clamp(self.microprice, self.bid, self.ask)
        total = self.bid_depth + self.ask_depth
        if total <= EPS:
            return self.mid
        # More bid depth shifts microprice toward the ask and vice versa.
        return clamp((self.ask * self.bid_depth + self.bid * self.ask_depth) / total,
                     self.bid, self.ask)


@dataclass(frozen=True)
class RewardContext:
    reward_qualified: bool = False
    max_spread_cents: float = 0.0
    min_size: float = 0.0
    pool_daily_usd: float = 0.0
    estimated_competitor_score: float = 0.0
    maker_rebate_fraction: float = 0.0
    taker_fee_rate: float = 0.0
    expected_filled_maker_share: float = 0.0


@dataclass(frozen=True)
class InventoryState:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    cash: float = 0.0
    sleeve_capital: float = 1.0

    @property
    def complete_sets(self) -> float:
        return max(0.0, min(self.yes_shares, self.no_shares))

    @property
    def residual_yes_shares(self) -> float:
        return self.yes_shares - self.no_shares


@dataclass(frozen=True)
class ExecutionEstimate:
    fill_probability: float
    adverse_markout_per_share: float
    fill_uncertainty: float = 1.0
    observations: int = 0
    fills: int = 0
    event_clusters: int = 0

    @property
    def mature(self) -> bool:
        return self.observations >= 50 and self.fills >= 20


@dataclass(frozen=True)
class QuoteEconomics:
    action: str
    outcome: str
    side: str
    price: float
    size: float
    queue_ahead: float
    fill_probability: float
    fair_value: float
    reservation_price: float
    gross_capture_per_share: float
    adverse_markout_per_share: float
    inventory_cost_per_share: float
    unwind_cost_per_share: float
    capital_latency_cost_per_share: float
    trading_edge_per_share: float
    maker_rebate_per_share: float
    expected_trading_pnl: float
    expected_rebate_pnl: float
    expected_liquidity_reward_pnl: float
    expected_total_pnl: float
    reward_score: float
    reward_qualified: bool
    subsidy_dependent: bool
    exploration: bool = False
    promotion_credit: bool = True
    reason: str = ""

    @property
    def total_ev_per_dollar(self) -> float:
        notional = max(EPS, self.price * self.size)
        return self.expected_total_pnl / notional


@dataclass(frozen=True)
class MakerDecision:
    market_id: str
    quotes: tuple[QuoteEconomics, ...] = field(default_factory=tuple)
    mode: str = "ABSTAIN"
    reason: str = ""
    fair_yes: float = 0.5
    residual_inventory_shares: float = 0.0


@dataclass(frozen=True)
class MakerPolicy:
    mid_weight: float = 0.40
    microprice_weight: float = 0.45
    flow_weight: float = 0.10
    related_market_weight: float = 0.05
    max_microstructure_shift_ticks: float = 2.0
    inventory_skew_strength: float = 1.0
    toxicity_withdraw_threshold: float = 0.75
    min_exploit_ev_per_dollar: float = 1e-5
    max_inside_ticks: int = 1
    reward_haircut: float = 0.50
    rebate_haircut: float = 0.50
    exploration_enabled: bool = True
    exploration_quote_notional_fraction: float = 0.001
    exploration_epsilon: float = 0.10
    max_order_fraction_of_sleeve: float = 0.01
    soft_inventory_fraction: float = 0.0125
    hard_inventory_fraction: float = 0.025
    cold_start_fill_prior: float = 0.02
    cold_start_adverse_markout_per_share: float = 0.002
    capital_cost_rate_annual: float = 0.05
    cancel_latency_ms: int = 100

    @classmethod
    def from_json(cls, path: Path) -> "MakerPolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        fv = raw.get("fair_value") or {}
        quoting = raw.get("quoting") or {}
        rewards = raw.get("rewards") or {}
        exploration = raw.get("exploration") or {}
        model = raw.get("execution_model") or {}
        risk = raw.get("risk") or {}
        inventory = raw.get("inventory") or {}
        latency = raw.get("latency") or {}
        return cls(
            mid_weight=finite(fv.get("mid_weight"), 0.40),
            microprice_weight=finite(fv.get("microprice_weight"), 0.45),
            flow_weight=finite(fv.get("flow_weight"), 0.10),
            related_market_weight=finite(fv.get("related_market_weight"), 0.05),
            max_microstructure_shift_ticks=finite(fv.get("max_microstructure_shift_ticks"), 2.0),
            inventory_skew_strength=finite(quoting.get("inventory_skew_strength"), 1.0),
            toxicity_withdraw_threshold=finite(quoting.get("toxicity_withdraw_threshold"), 0.75),
            min_exploit_ev_per_dollar=finite(quoting.get("minimum_exploit_ev_per_dollar"), 1e-5),
            max_inside_ticks=max(0, int(finite(quoting.get("max_inside_ticks"), 1))),
            reward_haircut=clamp(finite(rewards.get("reward_haircut_for_decisions"), 0.50), 0.0, 1.0),
            rebate_haircut=clamp(finite(rewards.get("rebate_haircut_for_decisions"), 0.50), 0.0, 1.0),
            exploration_enabled=bool(exploration.get("enabled", True)),
            exploration_quote_notional_fraction=max(0.0, finite(exploration.get("max_quote_notional_fraction"), 0.001)),
            exploration_epsilon=clamp(finite(exploration.get("epsilon"), 0.10), 0.0, 1.0),
            max_order_fraction_of_sleeve=max(0.0, finite(risk.get("max_order_fraction_of_sleeve"), 0.01)),
            soft_inventory_fraction=max(0.0, finite(inventory.get("soft_directional_inventory_fraction"), 0.0125)),
            hard_inventory_fraction=max(0.0, finite(inventory.get("max_directional_inventory_fraction"), 0.025)),
            cold_start_fill_prior=clamp(finite(model.get("cold_start_fill_prior"), 0.02), 0.0, 1.0),
            cold_start_adverse_markout_per_share=max(0.0, finite(model.get("cold_start_adverse_markout_per_share"), 0.002)),
            cancel_latency_ms=max(0, int(finite(quoting.get("cancel_latency_ms", latency.get("target_cancel_decision_p99_us", 100) / 1000), 100))),
        )


def reward_order_score(max_spread_cents: float, distance_cents: float, size: float,
                       multiplier: float = 1.0) -> float:
    """Official quadratic liquidity-reward order score S(v,s)*size."""
    v = max(0.0, float(max_spread_cents))
    s = max(0.0, float(distance_cents))
    q = max(0.0, float(size))
    if v <= EPS or s > v:
        return 0.0
    return ((v - s) / v) ** 2 * max(0.0, float(multiplier)) * q


def maker_fee_equivalent_per_share(price: float, taker_fee_rate: float) -> float:
    """Fee-equivalent basis used by Polymarket's maker-rebate allocation."""
    p = clamp(price, 0.0, 1.0)
    return max(0.0, taker_fee_rate) * p * (1.0 - p)


def expected_rebate_per_share(price: float, reward: RewardContext, policy: MakerPolicy) -> float:
    if reward.maker_rebate_fraction <= 0.0 or reward.taker_fee_rate <= 0.0:
        return 0.0
    # Exact daily allocation depends on our executed fee-equivalent share relative
    # to other makers.  The online selector supplies a conservative market-share
    # estimate; absent that evidence the rebate contribution is zero, never guessed.
    share = clamp(reward.expected_filled_maker_share, 0.0, 1.0)
    if share <= 0.0:
        return 0.0
    fee_equiv = maker_fee_equivalent_per_share(price, reward.taker_fee_rate)
    return fee_equiv * reward.maker_rebate_fraction * share * policy.rebate_haircut


def liquidity_reward_estimate(
    *,
    reward: RewardContext,
    price: float,
    mid: float,
    size: float,
    rest_seconds: float,
    companion_score: float,
    policy: MakerPolicy,
) -> tuple[float, float, bool]:
    """Conservative dollar reward estimate and raw score.

    Polymarket normalizes each maker's score against the market-wide score at
    sampled epochs.  We therefore only estimate dollars when both the pool and a
    positive competitor-score estimate are available.  Otherwise reward points
    remain observable but contribute zero dollars to trading decisions.
    """
    if not reward.reward_qualified or reward.max_spread_cents <= 0.0:
        return 0.0, 0.0, False
    distance_cents = abs(float(price) - float(mid)) * 100.0
    score = reward_order_score(reward.max_spread_cents, distance_cents, size)
    if score <= 0.0 or size + EPS < reward.min_size:
        return 0.0, score, False
    # Two-sided liquidity receives the strong score through min(Q1,Q2).  A lone
    # quote can still score in the central probability region but at 1/c.  The
    # caller passes the complementary-side score so we can approximate Q_min.
    c = 3.0
    if companion_score > 0.0:
        qmin = min(score, companion_score)
    elif 0.10 <= mid <= 0.90:
        qmin = score / c
    else:
        qmin = 0.0
    competition = max(0.0, reward.estimated_competitor_score)
    if qmin <= 0.0 or reward.pool_daily_usd <= 0.0 or competition <= 0.0:
        return 0.0, score, True
    share = qmin / (competition + qmin)
    dollars = reward.pool_daily_usd * share * max(0.0, rest_seconds) / 86400.0
    return dollars * policy.reward_haircut, score, True


def fair_value(
    book: BookState,
    policy: MakerPolicy,
    *,
    related_fair: float | None = None,
) -> float:
    book.validate()
    mid = book.mid
    micro = book.micro
    related = mid if related_fair is None else clamp(related_fair, book.bid, book.ask)
    # OFI is dimensionless in [-1,1].  Cap its contribution in ticks so a noisy
    # burst cannot turn the maker into a directional taker strategy.
    flow_shift = clamp(book.ofi, -1.0, 1.0) * book.tick_size * policy.max_microstructure_shift_ticks
    raw = (
        policy.mid_weight * mid
        + policy.microprice_weight * micro
        + policy.related_market_weight * related
        + policy.flow_weight * (mid + flow_shift)
    )
    total_weight = (
        policy.mid_weight + policy.microprice_weight + policy.related_market_weight + policy.flow_weight
    )
    if total_weight <= EPS:
        return mid
    return clamp(raw / total_weight, book.bid, book.ask)


def inventory_fraction(inventory: InventoryState, reference_price: float) -> float:
    capital = max(EPS, inventory.sleeve_capital)
    return inventory.residual_yes_shares * clamp(reference_price, 0.0, 1.0) / capital


def reservation_price(
    fair_yes: float,
    inventory: InventoryState,
    book: BookState,
    policy: MakerPolicy,
) -> float:
    frac = inventory_fraction(inventory, fair_yes)
    # Long YES inventory shifts reservation lower so we buy less YES / more NO.
    soft = max(EPS, policy.soft_inventory_fraction)
    normalized = clamp(frac / soft, -2.0, 2.0)
    shift = policy.inventory_skew_strength * normalized * max(book.tick_size, 0.25 * book.spread)
    return clamp(fair_yes - shift, book.bid, book.ask)


def post_only_price(book: BookState, side: str, action: str, max_inside_ticks: int = 1) -> float | None:
    book.validate()
    side = side.upper()
    action = action.upper()
    tick = book.tick_size
    if action == "WITHDRAW":
        return None
    if side == "BUY":
        if action == "JOIN" or action == "ONE_SIDED":
            price = book.bid
        elif action == "IMPROVE1":
            price = min(book.bid + min(1, max_inside_ticks) * tick, book.ask - tick)
        elif action == "FADE1":
            price = max(tick, book.bid - tick)
        elif action == "FADE2":
            price = max(tick, book.bid - 2.0 * tick)
        else:
            raise ValueError(f"unsupported_action:{action}")
        if price >= book.ask - EPS:
            return None
    elif side == "SELL":
        if action == "JOIN" or action == "ONE_SIDED":
            price = book.ask
        elif action == "IMPROVE1":
            price = max(book.ask - min(1, max_inside_ticks) * tick, book.bid + tick)
        elif action == "FADE1":
            price = min(1.0 - tick, book.ask + tick)
        elif action == "FADE2":
            price = min(1.0 - tick, book.ask + 2.0 * tick)
        else:
            raise ValueError(f"unsupported_action:{action}")
        if price <= book.bid + EPS:
            return None
    else:
        raise ValueError(f"unsupported_side:{side}")
    if not (0.0 < price < 1.0):
        return None
    # Respect the price grid conservatively.
    ticks = round(price / tick)
    price = clamp(ticks * tick, tick, 1.0 - tick)
    if side == "BUY" and price >= book.ask - EPS:
        return None
    if side == "SELL" and price <= book.bid + EPS:
        return None
    return price


def _inventory_cost_per_share(
    *,
    outcome: str,
    side: str,
    price: float,
    size: float,
    inventory: InventoryState,
    policy: MakerPolicy,
) -> float:
    outcome = outcome.upper()
    side = side.upper()
    direction = 1.0 if outcome == "YES" else -1.0
    if side == "SELL":
        direction *= -1.0
    capital = max(EPS, inventory.sleeve_capital)
    current = inventory.residual_yes_shares * price / capital
    after = (inventory.residual_yes_shares + direction * size) * price / capital
    hard = max(EPS, policy.hard_inventory_fraction)
    # Quadratic marginal inventory cost; actions reducing residual inventory earn
    # a negative cost (an economic bonus) rather than being artificially blocked.
    before_penalty = (abs(current) / hard) ** 2
    after_penalty = (abs(after) / hard) ** 2
    return 0.5 * max(price, 0.01) * (after_penalty - before_penalty) / max(size, 1.0)


def toxicity_score(book: BookState, estimate: ExecutionEstimate) -> float:
    spread = max(book.tick_size, book.spread)
    adverse_ratio = max(0.0, estimate.adverse_markout_per_share) / spread
    vol_ratio = max(0.0, book.short_volatility) / spread
    flow = abs(clamp(book.ofi, -1.0, 1.0))
    uncertainty = clamp(estimate.fill_uncertainty, 0.0, 1.0)
    return clamp(0.45 * adverse_ratio + 0.20 * vol_ratio + 0.20 * flow + 0.15 * uncertainty, 0.0, 2.0)


def evaluate_quote(
    *,
    outcome: str,
    side: str,
    action: str,
    book: BookState,
    fair: float,
    reservation: float,
    size: float,
    estimate: ExecutionEstimate,
    reward: RewardContext,
    companion_reward_score: float,
    inventory: InventoryState,
    policy: MakerPolicy,
    rest_seconds: float,
    unwind_cost_per_share: float = 0.0,
    latency_cost_per_share: float = 0.0,
    exploration: bool = False,
) -> QuoteEconomics | None:
    price = post_only_price(book, side, action, policy.max_inside_ticks)
    if price is None or size <= 0.0:
        return None
    side = side.upper()
    if side == "BUY":
        capture = reservation - price
        queue = max(0.0, book.queue_bid if abs(price - book.bid) <= book.tick_size / 2 else 0.0)
    else:
        capture = price - reservation
        queue = max(0.0, book.queue_ask if abs(price - book.ask) <= book.tick_size / 2 else 0.0)

    fill_p = clamp(estimate.fill_probability, 0.0, 1.0)
    inventory_cost = _inventory_cost_per_share(
        outcome=outcome, side=side, price=price, size=size, inventory=inventory, policy=policy
    )
    capital_cost = max(0.0, policy.capital_cost_rate_annual) * max(0.0, rest_seconds) / (365.25 * 86400.0) * price
    capital_latency = capital_cost + max(0.0, latency_cost_per_share)
    trading_edge = (
        capture
        - max(0.0, estimate.adverse_markout_per_share)
        - inventory_cost
        - max(0.0, unwind_cost_per_share)
        - capital_latency
    )
    rebate = expected_rebate_per_share(price, reward, policy)
    liquidity_reward, reward_score, reward_qualified = liquidity_reward_estimate(
        reward=reward,
        price=price,
        mid=book.mid,
        size=size,
        rest_seconds=rest_seconds,
        companion_score=companion_reward_score,
        policy=policy,
    )
    trading_pnl = fill_p * size * trading_edge
    rebate_pnl = fill_p * size * rebate
    total = trading_pnl + rebate_pnl + liquidity_reward
    subsidy_dependent = trading_pnl <= 0.0 < total
    return QuoteEconomics(
        action=action.upper(), outcome=outcome.upper(), side=side, price=price,
        size=size, queue_ahead=queue, fill_probability=fill_p, fair_value=fair,
        reservation_price=reservation, gross_capture_per_share=capture,
        adverse_markout_per_share=max(0.0, estimate.adverse_markout_per_share),
        inventory_cost_per_share=inventory_cost,
        unwind_cost_per_share=max(0.0, unwind_cost_per_share),
        capital_latency_cost_per_share=capital_latency,
        trading_edge_per_share=trading_edge,
        maker_rebate_per_share=rebate,
        expected_trading_pnl=trading_pnl,
        expected_rebate_pnl=rebate_pnl,
        expected_liquidity_reward_pnl=liquidity_reward,
        expected_total_pnl=total,
        reward_score=reward_score,
        reward_qualified=reward_qualified,
        subsidy_dependent=subsidy_dependent,
        exploration=exploration,
        promotion_credit=not exploration,
        reason="exploration_probe" if exploration else "ev_optimized",
    )


def _quote_size(book: BookState, inventory: InventoryState, policy: MakerPolicy, exploration: bool) -> float:
    capital = max(0.0, inventory.sleeve_capital)
    fraction = policy.exploration_quote_notional_fraction if exploration else policy.max_order_fraction_of_sleeve
    notional = capital * max(0.0, fraction)
    reference = max(book.bid, book.tick_size)
    raw = notional / reference if reference > 0.0 else 0.0
    # Depth does not grant size; it only caps a risk/capital-derived target.
    depth_cap = max(1.0, 0.10 * max(book.bid_depth, book.ask_depth, 1.0))
    return max(0.0, min(raw, depth_cap))


def choose_outcome_quote(
    *,
    outcome: str,
    book: BookState,
    fair: float,
    reservation: float,
    estimate: ExecutionEstimate,
    reward: RewardContext,
    inventory: InventoryState,
    policy: MakerPolicy,
    companion_reward_score: float = 0.0,
    allow_sell: bool = False,
    sell_inventory: float = 0.0,
) -> QuoteEconomics | None:
    toxic = toxicity_score(book, estimate)
    if toxic >= policy.toxicity_withdraw_threshold:
        return None

    mature = estimate.mature
    exploration = not mature and policy.exploration_enabled
    size = _quote_size(book, inventory, policy, exploration)
    if size <= 0.0:
        return None
    if exploration:
        # Cold start is intentionally small.  We evaluate actions with conservative
        # priors but do not require positive EV or grant promotion credit.
        estimate = ExecutionEstimate(
            fill_probability=max(policy.cold_start_fill_prior, min(estimate.fill_probability, 0.25)),
            adverse_markout_per_share=max(policy.cold_start_adverse_markout_per_share,
                                          estimate.adverse_markout_per_share),
            fill_uncertainty=max(estimate.fill_uncertainty, 0.75),
            observations=estimate.observations,
            fills=estimate.fills,
            event_clusters=estimate.event_clusters,
        )

    actions = ("JOIN", "IMPROVE1", "FADE1", "FADE2")
    candidates: list[QuoteEconomics] = []
    for action in actions:
        candidate = evaluate_quote(
            outcome=outcome, side="BUY", action=action, book=book, fair=fair,
            reservation=reservation, size=size, estimate=estimate, reward=reward,
            companion_reward_score=companion_reward_score, inventory=inventory,
            policy=policy, rest_seconds=1.0, exploration=exploration,
        )
        if candidate is not None:
            candidates.append(candidate)
    if allow_sell and sell_inventory > EPS:
        sell_size = min(size, sell_inventory)
        for action in actions:
            candidate = evaluate_quote(
                outcome=outcome, side="SELL", action=action, book=book, fair=fair,
                reservation=reservation, size=sell_size, estimate=estimate, reward=reward,
                companion_reward_score=companion_reward_score, inventory=inventory,
                policy=policy, rest_seconds=1.0, exploration=exploration,
            )
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        return None

    if exploration:
        # Information/reward-aware cold-start ordering: JOIN provides realistic queue
        # evidence, FADE samples lower toxicity, IMPROVE samples higher fill hazard.
        priority = {"JOIN": 3.0, "FADE1": 2.0, "IMPROVE1": 1.0, "FADE2": 0.5}
        return max(candidates, key=lambda q: (q.reward_score + priority.get(q.action, 0.0), q.expected_total_pnl))

    best = max(candidates, key=lambda q: q.expected_total_pnl)
    if best.expected_total_pnl <= 0.0 or best.total_ev_per_dollar < policy.min_exploit_ev_per_dollar:
        return None
    return best


def choose_binary_market_quotes(
    *,
    market_id: str,
    yes_book: BookState,
    no_book: BookState,
    yes_estimate: ExecutionEstimate,
    no_estimate: ExecutionEstimate,
    reward: RewardContext,
    inventory: InventoryState,
    policy: MakerPolicy,
    related_yes_fair: float | None = None,
) -> MakerDecision:
    yes_book.validate()
    no_book.validate()
    fair_yes = fair_value(yes_book, policy, related_fair=related_yes_fair)
    # Complement consistency is enforced at the decision layer rather than fitting
    # two unrelated directional models.
    raw_fair_no = fair_value(no_book, policy, related_fair=None)
    complement_yes = 1.0 - raw_fair_no
    fair_yes = clamp(0.5 * (fair_yes + complement_yes), yes_book.bid, yes_book.ask)
    fair_no = 1.0 - fair_yes
    reserve_yes = reservation_price(fair_yes, inventory, yes_book, policy)

    # For NO inventory, flip the residual sign so long YES encourages NO purchases.
    inverse_inventory = InventoryState(
        yes_shares=inventory.no_shares,
        no_shares=inventory.yes_shares,
        cash=inventory.cash,
        sleeve_capital=inventory.sleeve_capital,
    )
    reserve_no = reservation_price(fair_no, inverse_inventory, no_book, policy)

    # Pre-compute approximate reward score for a JOIN quote on the opposite outcome;
    # this lets liquidity-reward economics reflect the two-sided Q_min incentive.
    yes_join = post_only_price(yes_book, "BUY", "JOIN", policy.max_inside_ticks)
    no_join = post_only_price(no_book, "BUY", "JOIN", policy.max_inside_ticks)
    yes_size = _quote_size(yes_book, inventory, policy, not yes_estimate.mature)
    no_size = _quote_size(no_book, inventory, policy, not no_estimate.mature)
    yes_companion = reward_order_score(
        reward.max_spread_cents,
        abs((no_join if no_join is not None else no_book.bid) - no_book.mid) * 100.0,
        no_size,
    ) if reward.reward_qualified else 0.0
    no_companion = reward_order_score(
        reward.max_spread_cents,
        abs((yes_join if yes_join is not None else yes_book.bid) - yes_book.mid) * 100.0,
        yes_size,
    ) if reward.reward_qualified else 0.0

    yes_quote = choose_outcome_quote(
        outcome="YES", book=yes_book, fair=fair_yes, reservation=reserve_yes,
        estimate=yes_estimate, reward=reward, inventory=inventory, policy=policy,
        companion_reward_score=yes_companion, allow_sell=inventory.yes_shares > EPS,
        sell_inventory=inventory.yes_shares,
    )
    no_quote = choose_outcome_quote(
        outcome="NO", book=no_book, fair=fair_no, reservation=reserve_no,
        estimate=no_estimate, reward=reward, inventory=inventory, policy=policy,
        companion_reward_score=no_companion, allow_sell=inventory.no_shares > EPS,
        sell_inventory=inventory.no_shares,
    )
    quotes = tuple(q for q in (yes_quote, no_quote) if q is not None)
    if not quotes:
        return MakerDecision(
            market_id=market_id, quotes=(), mode="ABSTAIN", reason="no_positive_or_safe_quote",
            fair_yes=fair_yes, residual_inventory_shares=inventory.residual_yes_shares,
        )
    if any(q.exploration for q in quotes):
        mode = "EXPLORE"
        reason = "cold_start_execution_learning"
    elif any(q.subsidy_dependent for q in quotes):
        mode = "SUBSIDY_AWARE"
        reason = "positive_total_ev_with_reward_or_rebate_dependency"
    else:
        mode = "EXPLOIT"
        reason = "positive_fill_conditioned_trading_ev"
    return MakerDecision(
        market_id=market_id, quotes=quotes, mode=mode, reason=reason,
        fair_yes=fair_yes, residual_inventory_shares=inventory.residual_yes_shares,
    )


def summarize_decision(decision: MakerDecision) -> dict[str, Any]:
    return {
        "market_id": decision.market_id,
        "mode": decision.mode,
        "reason": decision.reason,
        "fair_yes": decision.fair_yes,
        "residual_inventory_shares": decision.residual_inventory_shares,
        "quotes": [q.__dict__ for q in decision.quotes],
    }


__all__ = [
    "BookState", "RewardContext", "InventoryState", "ExecutionEstimate",
    "QuoteEconomics", "MakerDecision", "MakerPolicy", "reward_order_score",
    "maker_fee_equivalent_per_share", "expected_rebate_per_share",
    "liquidity_reward_estimate", "fair_value", "reservation_price",
    "post_only_price", "toxicity_score", "evaluate_quote",
    "choose_outcome_quote", "choose_binary_market_quotes", "summarize_decision",
]
