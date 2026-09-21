"""Direct-action value learning for Polymarket PAPER research.

The model learns total executable PnL directly as a function of causal market
state and an action (size, exit horizon) at a declared execution latency.  It
never estimates an asset mean vector or covariance matrix.

Scope is intentionally narrow and fail-closed:
- PAPER / zero authority only.
- Entry uses the observed post-latency L1 ask with zero chase.
- Candidate size cannot exceed decision-time visible L1 depth, venue minimum,
  available capital, or the hard per-order notional cap.
- Missing arrival/exit evidence is censored, never converted to zero.
- We do not pretend to know counterfactual impact beyond L1.  Size-dependent
  market impact requires richer depth data and remains outside this model.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import argparse
import math
from pathlib import Path

from research.walk_forward_v2.core import (
    SAFETY,
    arrival,
    atomic_json,
    build_dataset,
    fee_per_share,
    feature_names,
    finite,
    folds,
)

SCHEMA = "polymarket_direct_action_value_v3"
DEFAULT_SIZE_GRID = (1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0)
DEFAULT_ACTION_HORIZONS_MS = (50, 100, 250, 500, 1000, 2000)
DEFAULT_TRAIN_LATENCIES_MS = (25, 50, 100, 250)
DEFAULT_ENTRY_CAP = 0.80
DEFAULT_HARD_ORDER_NOTIONAL = 100.0
DEFAULT_MINIMUM_TTE_NS = 30_000_000_000
DEFAULT_MAXIMUM_TTE_NS = 120_000_000_000
LIVE_MINIMUM_TTE_NS = 105_000_000_000
LIVE_MAXIMUM_TTE_NS = 120_000_000_000


@dataclass(frozen=True)
class FrictionPolicy:
    """Residual policy frictions not already embedded in executable cash PnL.

    Spread, post-signal price drift/slippage, fill/no-fill, taker fees and
    adverse post-fill repricing are learned from executable observations and
    therefore must not be subtracted a second time here.
    """
    capital_charge_bps_per_second: float = 0.0
    asset_concentration_lambda: float = 0.0
    common_factor_concentration_lambda: float = 0.0
    uncertainty_aversion: float = 1.0

    def validated(self):
        values = (
            self.capital_charge_bps_per_second,
            self.asset_concentration_lambda,
            self.common_factor_concentration_lambda,
            self.uncertainty_aversion,
        )
        if any(not finite(v) or float(v) < 0 for v in values):
            raise ValueError("nonnegative finite friction policy required")
        return self


DEFAULT_FRICTION_POLICY = FrictionPolicy()


def _quantile(values, level):
    values = sorted(float(v) for v in values if finite(v))
    if not values:
        return None
    index = int(math.ceil(level * len(values))) - 1
    return values[max(0, min(len(values) - 1, index))]


def _valid_state(row, *, minimum_tte_ns=DEFAULT_MINIMUM_TTE_NS,
                 maximum_tte_ns=DEFAULT_MAXIMUM_TTE_NS):
    return (
        row.get("signal_valid") is True
        and row.get("confirmed") is True
        and row.get("book_valid") is True
        and row.get("pretrigger") is True
        and finite(row.get("ask")) and finite(row.get("bid"))
        and 0 < float(row["bid"]) < float(row["ask"]) < 1
        and finite(row.get("quantity")) and float(row["quantity"]) > 0
        and finite(row.get("minimum")) and float(row["minimum"]) > 0
        and isinstance(row.get("tte_ns"), int)
        and minimum_tte_ns <= row["tte_ns"] <= maximum_tte_ns
    )


def selected_action_side(row):
    token = str(row.get("token_id") or "")
    yes = str(row.get("yes_token_id") or "")
    no = str(row.get("no_token_id") or "")
    if yes and token == yes:
        return "YES"
    if no and token == no:
        return "NO"
    return "SELECTED"


def action_side_sign(row, side):
    if side == "YES":
        return 1.0
    if side == "NO":
        return -1.0
    direction = float(row.get("direction") or 0)
    return 1.0 if direction >= 0 else -1.0


def decision_side_state(row, side=None):
    side = side or selected_action_side(row)
    pair = row.get("pair")
    if (
        side in ("YES", "NO")
        and isinstance(pair, dict)
        and pair.get("state") == "BILATERAL_EXECUTABLE_READY"
    ):
        state = pair.get(side.lower())
        if isinstance(state, dict):
            return {
                "bid": float(state["bid"]),
                "ask": float(state["ask"]),
                "bid_quantity": float(state.get("bid_quantity") or 0.0),
                "ask_quantity": float(state.get("ask_quantity") or 0.0),
            }
    selected = selected_action_side(row)
    if side == "SELECTED" or side == selected:
        return {
            "bid": float(row["bid"]),
            "ask": float(row["ask"]),
            "bid_quantity": float(row.get("bid_quantity") or 0.0),
            "ask_quantity": float(row.get("quantity") or 0.0),
        }
    return None


def observed_side_state(container, side, row):
    pair = container.get("pair") if isinstance(container, dict) else None
    if (
        side in ("YES", "NO")
        and isinstance(pair, dict)
        and pair.get("state") == "BILATERAL_EXECUTABLE_READY"
    ):
        state = pair.get(side.lower())
        if isinstance(state, dict):
            return {
                "bid": float(state["bid"]),
                "ask": float(state["ask"]),
                "bid_quantity": float(state.get("bid_quantity") or 0.0),
                "ask_quantity": float(state.get("ask_quantity") or 0.0),
            }
    selected = selected_action_side(row)
    if side == "SELECTED" or side == selected:
        bid = container.get("bid", container.get("arrival_bid"))
        ask = container.get("ask", container.get("arrival_ask"))
        quantity = container.get("quantity", container.get("arrival_quantity"))
        bid_quantity = container.get(
            "bid_quantity", container.get("arrival_bid_quantity"))
        if finite(bid) and finite(ask):
            return {
                "bid": float(bid), "ask": float(ask),
                "bid_quantity": float(bid_quantity or 0.0),
                "ask_quantity": float(quantity or 0.0),
            }
    return None


def decision_action_sides(row):
    pair = row.get("pair")
    if (
        isinstance(pair, dict)
        and pair.get("state") == "BILATERAL_EXECUTABLE_READY"
        and all(
            isinstance(pair.get(side), dict)
            and float(pair[side].get("ask_quantity") or 0) > 0
            for side in ("yes", "no")
        )
    ):
        return ("YES", "NO")
    return (selected_action_side(row),)


def candidate_sizes(row, *, side=None, size_grid=DEFAULT_SIZE_GRID,
                    hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                    available_capital=None, max_sizes=5):
    """Decision-time feasible size support.  No future depth enters here."""
    if not _valid_state(row):
        return []
    state = decision_side_state(row, side)
    if state is None:
        return []
    ask = float(state["ask"])
    minimum = float(row["minimum"])
    depth = float(state["ask_quantity"])
    cap = float(hard_order_notional)
    if available_capital is not None:
        cap = min(cap, max(0.0, float(available_capital)))
    if cap <= 0 or ask <= 0:
        return []
    maximum = min(depth, cap / ask)
    if maximum + 1e-12 < minimum:
        return []
    values = {
        float(size) for size in size_grid
        if finite(size) and minimum <= float(size) <= maximum + 1e-12
    }
    # The observed capacity endpoint is a legitimate action.  It is not a claim
    # about depth beyond L1.
    values.add(float(maximum))
    ordered = sorted(values)
    if len(ordered) <= max_sizes:
        return ordered
    # Deterministic thinning keeps the action expansion bounded.
    indices = {
        round(i * (len(ordered) - 1) / (max_sizes - 1))
        for i in range(max_sizes)
    }
    return [ordered[i] for i in sorted(indices)]


def action_execution_kernel(
    row,
    *,
    horizon_ms,
    latency_ms,
    side=None,
    entry_cap=DEFAULT_ENTRY_CAP,
):
    """Size-independent causal execution kernel for one state/side/horizon.

    All book lookup, zero-chase, fee and exit economics are computed once.
    Quantity enters later through observed entry ask capacity and future exit
    bid capacity. Residual inventory at the chosen horizon is never assigned
    fictitious liquidity.
    """
    if horizon_ms <= latency_ms:
        return None, "HORIZON_NOT_AFTER_EXECUTION"
    if not _valid_state(row):
        return None, "STATE_OUTSIDE_RESEARCH_SUPPORT"
    side = side or selected_action_side(row)
    decision_state = decision_side_state(row, side)
    if decision_state is None:
        return None, "SIDE_DECISION_EVIDENCE_UNAVAILABLE"
    ask0 = float(decision_state["ask"])
    bid0 = float(decision_state["bid"])
    if ask0 > float(entry_cap) + 1e-12:
        return None, "ENTRY_CAP"

    book, why = arrival(row, int(latency_ms))
    if book is None:
        return None, str(why or "ARRIVAL_UNAVAILABLE")
    arrival_state = observed_side_state(book, side, row)
    if arrival_state is None:
        return None, "SIDE_ARRIVAL_EVIDENCE_UNAVAILABLE"

    # Zero chase is size-independent.
    if float(arrival_state["ask"]) > ask0 + 1e-12:
        return {
            "side": side,
            "state": "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED",
            "fill_capacity": 0.0,
            "exit_capacity": 0.0,
            "decision_ask": ask0,
            "decision_bid": bid0,
            "entry_price": None,
            "exit_bid": None,
            "cash_pnl_per_share": 0.0,
            "gross_executable_markout_per_share": 0.0,
            "entry_fee_per_share": 0.0,
            "exit_fee_per_share": 0.0,
            "decision_half_spread_per_share": 0.0,
            "latency_price_drift_per_share": 0.0,
            "exit_half_spread_per_share": 0.0,
            "ideal_midpoint_alpha_per_share": None,
            "future_mid": None,
        }, "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"

    fill_capacity = float(arrival_state.get("ask_quantity") or 0.0)
    if fill_capacity <= 0:
        return {
            "side": side,
            "state": "OBSERVED_NO_FILL_ZERO_DEPTH",
            "fill_capacity": 0.0,
            "exit_capacity": 0.0,
            "decision_ask": ask0,
            "decision_bid": bid0,
            "entry_price": None,
            "exit_bid": None,
            "cash_pnl_per_share": 0.0,
            "gross_executable_markout_per_share": 0.0,
            "entry_fee_per_share": 0.0,
            "exit_fee_per_share": 0.0,
            "decision_half_spread_per_share": 0.0,
            "latency_price_drift_per_share": 0.0,
            "exit_half_spread_per_share": 0.0,
            "ideal_midpoint_alpha_per_share": None,
            "future_mid": None,
        }, "OBSERVED_NO_FILL_ZERO_DEPTH"

    target = row.get("targets", {}).get(str(int(horizon_ms)), {})
    if target.get("state") != "OBSERVED":
        return None, "EXIT_EVIDENCE_UNAVAILABLE"
    exit_state = observed_side_state(target, side, row)
    if exit_state is None:
        return None, "SIDE_EXIT_EVIDENCE_UNAVAILABLE"

    entry_price = float(arrival_state["ask"])
    exit_bid = float(exit_state["bid"])
    exit_capacity = max(0.0, float(exit_state.get("bid_quantity") or 0.0))
    entry_fee = fee_per_share(row, entry_price)
    exit_fee = fee_per_share(row, exit_bid)
    gross = exit_bid - entry_price
    cash = gross - entry_fee - exit_fee

    decision_mid = (bid0 + ask0) / 2.0
    future_ask = exit_state.get("ask")
    future_mid = (
        (exit_bid + float(future_ask)) / 2.0
        if finite(future_ask) and float(future_ask) > exit_bid
        else None
    )
    ideal_alpha = (
        future_mid - decision_mid if future_mid is not None else None
    )
    decision_half_spread = ask0 - decision_mid
    latency_drift = entry_price - ask0
    exit_half_spread = (
        future_mid - exit_bid if future_mid is not None else None
    )
    return {
        "side": side,
        "state": "OBSERVED_EXECUTABLE",
        "fill_capacity": fill_capacity,
        "exit_capacity": exit_capacity,
        "decision_ask": ask0,
        "decision_bid": bid0,
        "entry_price": entry_price,
        "exit_bid": exit_bid,
        "cash_pnl_per_share": float(cash),
        "gross_executable_markout_per_share": float(gross),
        "entry_fee_per_share": float(entry_fee),
        "exit_fee_per_share": float(exit_fee),
        "decision_half_spread_per_share": float(decision_half_spread),
        "latency_price_drift_per_share": float(latency_drift),
        "exit_half_spread_per_share": (
            float(exit_half_spread) if exit_half_spread is not None else None
        ),
        "ideal_midpoint_alpha_per_share": (
            float(ideal_alpha) if ideal_alpha is not None else None
        ),
        "future_mid": float(future_mid) if future_mid is not None else None,
    }, "OBSERVED_EXECUTABLE"


def economics_from_execution_kernel(kernel, size):
    """Apply quantity with conservative, capacity-aware forced exit.

    Entry fill is bounded by observed arrival ask depth. At the selected exit
    horizon, only observed bid depth is executable. Any residual inventory is
    assigned terminal value zero in this static target, yielding a conservative
    lower bound rather than inventing unobserved exit liquidity.
    """
    size = float(size)
    fill = min(size, max(0.0, float(kernel["fill_capacity"])))
    side = str(kernel["side"])
    if fill <= 0:
        return {
            "side": side,
            "cash_pnl": 0.0,
            "gross_executable_markout": 0.0,
            "filled": 0.0,
            "exit_filled": 0.0,
            "residual_inventory": 0.0,
            "fully_exitable_at_horizon": True,
            "requested": size,
            "entry_price": None,
            "exit_bid": None,
            "exit_bid_quantity": 0.0,
            "entry_fee": 0.0,
            "exit_fee": 0.0,
            "total_fees": 0.0,
            "decision_half_spread_cost": 0.0,
            "latency_price_drift_cost": 0.0,
            "exit_half_spread_cost": 0.0,
            "exit_liquidity_shortfall_cost": 0.0,
            "ideal_midpoint_alpha": None,
            "residual_terminal_value_assumption": "ZERO_WORST_CASE",
            "frictions_embedded_in_cash_pnl": True,
        }, str(kernel["state"])

    exit_capacity = max(0.0, float(kernel.get("exit_capacity") or 0.0))
    exit_fill = min(fill, exit_capacity)
    residual = max(0.0, fill - exit_fill)
    entry_price = float(kernel["entry_price"])
    exit_bid = float(kernel["exit_bid"])
    entry_fee = fill * float(kernel["entry_fee_per_share"])
    exit_fee = exit_fill * float(kernel["exit_fee_per_share"])
    gross = exit_fill * exit_bid - fill * entry_price
    cash = gross - entry_fee - exit_fee

    # Preserve the historical entry-fill state taxonomy for funnel parity.
    # Exit feasibility is orthogonal and recorded explicitly below.
    state = (
        "OBSERVED_FULL_FILL"
        if fill + 1e-12 >= size
        else "OBSERVED_PARTIAL_FILL"
    )

    exit_half = kernel["exit_half_spread_per_share"]
    ideal_alpha = kernel["ideal_midpoint_alpha_per_share"]
    future_mid = kernel.get("future_mid")
    exit_liquidity_shortfall = (
        residual * float(future_mid) if finite(future_mid) else None
    )
    return {
        "side": side,
        "cash_pnl": float(cash),
        "gross_executable_markout": float(gross),
        "filled": float(fill),
        "exit_filled": float(exit_fill),
        "residual_inventory": float(residual),
        "fully_exitable_at_horizon": residual <= 1e-12,
        "requested": size,
        "entry_price": entry_price,
        "exit_bid": exit_bid,
        "exit_bid_quantity": float(exit_capacity),
        "entry_fee": float(entry_fee),
        "exit_fee": float(exit_fee),
        "total_fees": float(entry_fee + exit_fee),
        "decision_half_spread_cost": float(
            fill * kernel["decision_half_spread_per_share"]),
        "latency_price_drift_cost": float(
            fill * kernel["latency_price_drift_per_share"]),
        "exit_half_spread_cost": (
            float(exit_fill * exit_half) if exit_half is not None else None
        ),
        "exit_liquidity_shortfall_cost": (
            float(exit_liquidity_shortfall)
            if exit_liquidity_shortfall is not None else None
        ),
        "ideal_midpoint_alpha": (
            float(fill * ideal_alpha) if ideal_alpha is not None else None
        ),
        "residual_terminal_value_assumption": "ZERO_WORST_CASE",
        "frictions_embedded_in_cash_pnl": True,
    }, state


def realized_action_economics(row, *, size, horizon_ms, latency_ms, side=None,
                              entry_cap=DEFAULT_ENTRY_CAP,
                              hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                              require_full_decision_depth=True):
    """Observed total executable economics, implemented through one kernel."""
    if not _valid_state(row):
        return None, "STATE_OUTSIDE_RESEARCH_SUPPORT"
    side = side or selected_action_side(row)
    decision_state = decision_side_state(row, side)
    if decision_state is None:
        return None, "SIDE_DECISION_EVIDENCE_UNAVAILABLE"

    size = float(size)
    ask0 = float(decision_state["ask"])
    decision_depth = float(decision_state["ask_quantity"])
    if size + 1e-12 < float(row["minimum"]) or size <= 0:
        return None, "BELOW_VENUE_MINIMUM"
    if require_full_decision_depth and size > decision_depth + 1e-12:
        return None, "INSUFFICIENT_DECISION_DEPTH"
    if size * ask0 > float(hard_order_notional) + 1e-9:
        return None, "ORDER_NOTIONAL_CAP"

    kernel, state = action_execution_kernel(
        row,
        horizon_ms=horizon_ms,
        latency_ms=latency_ms,
        side=side,
        entry_cap=entry_cap,
    )
    if kernel is None:
        return None, state
    return economics_from_execution_kernel(kernel, size)


def realized_action_value(row, *, size, horizon_ms, latency_ms, side=None,
                          entry_cap=DEFAULT_ENTRY_CAP,
                          hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                          require_full_decision_depth=True):
    economics, state = realized_action_economics(
        row, size=size, horizon_ms=horizon_ms, latency_ms=latency_ms, side=side,
        entry_cap=entry_cap, hard_order_notional=hard_order_notional,
        require_full_decision_depth=require_full_decision_depth)
    return (None if economics is None else float(economics["cash_pnl"])), state


def residual_policy_friction(action, row, *, portfolio_state=None,
                             capital_budget=10_000.0,
                             friction_policy=DEFAULT_FRICTION_POLICY):
    """Incremental non-execution friction for policy selection.

    This is deliberately outside the learned executable cash target to avoid
    double-counting spread/fees/slippage.  Portfolio penalties use only current
    state and the candidate action; no mean/covariance estimate is introduced.
    """
    policy = friction_policy.validated()
    budget = max(1e-12, float(capital_budget))
    notional = max(0.0, float(action.get("notional") or 0.0))
    horizon_seconds = max(0.0, float(action.get("exit_horizon_ms") or 0.0) / 1000.0)
    capital_lock = (
        notional * float(policy.capital_charge_bps_per_second) * 1e-4
        * horizon_seconds
    )

    state = portfolio_state or {}
    asset_signed = state.get("asset_signed_notional") or {}
    asset = str(row.get("asset") or "UNKNOWN")
    side = str(action.get("side") or selected_action_side(row))
    direction = action_side_sign(row, side)
    signed = direction * notional

    current_asset = float(asset_signed.get(asset, 0.0) or 0.0)
    current_factor = float(state.get("common_factor_signed_notional") or 0.0)
    asset_increment = (
        (current_asset + signed) ** 2 - current_asset ** 2
    ) / budget
    factor_increment = (
        (current_factor + signed) ** 2 - current_factor ** 2
    ) / budget
    asset_penalty = max(
        0.0, float(policy.asset_concentration_lambda) * asset_increment)
    factor_penalty = max(
        0.0, float(policy.common_factor_concentration_lambda) * factor_increment)
    return {
        "capital_lock_penalty": float(capital_lock),
        "asset_concentration_penalty": float(asset_penalty),
        "common_factor_concentration_penalty": float(factor_penalty),
        "total_residual_friction": float(
            capital_lock + asset_penalty + factor_penalty),
    }


class StreamingRidge:
    """Exact ridge normal equations from bounded-memory sufficient statistics.

    This matches the train-only preprocessing contract used by the existing
    Ridge model: finite-value mean centering, half-range scaling, center
    imputation for missing values, plus one missingness indicator per feature.

    No N x p design matrix is materialized.  One pass over a re-iterable action
    factory is enough to compute the standardized Gram matrix exactly.
    """

    def __init__(self, names, *, ridge=8.0, batch_size=4096):
        self.names = tuple(names)
        self.ridge = float(ridge)
        self.batch_size = int(batch_size)
        if not self.names or not finite(self.ridge) or self.ridge <= 0:
            raise ValueError("valid streaming ridge specification required")
        if self.batch_size <= 0:
            raise ValueError("positive streaming ridge batch size required")

    def fit_factory(self, factory, target):
        import numpy as np

        p = len(self.names)
        sum_x = np.zeros(p, dtype=np.float64)
        count = np.zeros(p, dtype=np.float64)
        minimum = np.full(p, np.inf, dtype=np.float64)
        maximum = np.full(p, -np.inf, dtype=np.float64)
        xx = np.zeros((p, p), dtype=np.float64)
        xm = np.zeros((p, p), dtype=np.float64)
        mm = np.zeros((p, p), dtype=np.float64)
        xy = np.zeros(p, dtype=np.float64)
        my = np.zeros(p, dtype=np.float64)
        sum_y = 0.0
        sum_y2 = 0.0
        n = 0

        # Compensated accumulation across BLAS blocks.  The expensive work is
        # dense p x p matrix multiplication; memory stays O(batch*p + p^2).
        xx_c = np.zeros_like(xx)
        xm_c = np.zeros_like(xm)
        mm_c = np.zeros_like(mm)
        xy_c = np.zeros_like(xy)
        my_c = np.zeros_like(my)
        sum_x_c = np.zeros_like(sum_x)
        count_c = np.zeros_like(count)

        def kahan_add(total, compensation, increment):
            y = increment - compensation
            updated = total + y
            compensation[...] = (updated - total) - y
            total[...] = updated

        def consume(rows):
            nonlocal sum_y, sum_y2, n
            if not rows:
                return
            m = len(rows)
            raw = np.zeros((m, p), dtype=np.float64)
            mask = np.zeros((m, p), dtype=np.float64)
            y = np.empty(m, dtype=np.float64)
            for i, row in enumerate(rows):
                features = row["features"]
                for j, name in enumerate(self.names):
                    value = features.get(name)
                    if finite(value):
                        raw[i, j] = float(value)
                        mask[i, j] = 1.0
                value = target(row)
                if not finite(value):
                    raise ValueError("nonfinite streaming ridge target")
                y[i] = float(value)

            block_sum = raw.sum(axis=0)
            block_count = mask.sum(axis=0)
            kahan_add(sum_x, sum_x_c, block_sum)
            kahan_add(count, count_c, block_count)
            kahan_add(xx, xx_c, raw.T @ raw)
            kahan_add(xm, xm_c, raw.T @ mask)
            kahan_add(mm, mm_c, mask.T @ mask)
            kahan_add(xy, xy_c, raw.T @ y)
            kahan_add(my, my_c, mask.T @ y)

            observed = mask.astype(bool)
            block_min = np.where(observed, raw, np.inf).min(axis=0)
            block_max = np.where(observed, raw, -np.inf).max(axis=0)
            minimum[:] = np.minimum(minimum, block_min)
            maximum[:] = np.maximum(maximum, block_max)
            sum_y += float(y.sum())
            sum_y2 += float(y @ y)
            n += m

        batch = []
        for row in factory():
            batch.append(row)
            if len(batch) >= self.batch_size:
                consume(batch)
                batch.clear()
        consume(batch)
        if n == 0:
            raise ValueError("streaming ridge received zero rows")

        center = np.divide(
            sum_x, count, out=np.zeros_like(sum_x), where=count > 0)
        scale = np.ones(p, dtype=np.float64)
        observed_features = count > 0
        scale[observed_features] = np.maximum(
            1e-9,
            (maximum[observed_features] - minimum[observed_features]) / 2.0,
        )

        # z_j = m_j (x_j-c_j)/s_j, d_j = 1-m_j.
        centered_xx = (
            xx
            - xm * center[None, :]
            - xm.T * center[:, None]
            + mm * center[:, None] * center[None, :]
        )
        zz = centered_xx / (scale[:, None] * scale[None, :])
        zz = (zz + zz.T) * 0.5

        zd_numerator = (
            sum_x[:, None] - xm
            - center[:, None] * (count[:, None] - mm)
        )
        zd = zd_numerator / scale[:, None]
        dd = (
            float(n)
            - count[:, None]
            - count[None, :]
            + mm
        )

        intercept_z = (sum_x - center * count) / scale
        intercept_d = float(n) - count

        dimension = 1 + 2 * p
        gram = np.zeros((dimension, dimension), dtype=np.float64)
        gram[0, 0] = float(n)
        gram[0, 1:1+p] = intercept_z
        gram[1:1+p, 0] = intercept_z
        gram[0, 1+p:] = intercept_d
        gram[1+p:, 0] = intercept_d
        gram[1:1+p, 1:1+p] = zz
        gram[1:1+p, 1+p:] = zd
        gram[1+p:, 1:1+p] = zd.T
        gram[1+p:, 1+p:] = dd
        gram = (gram + gram.T) * 0.5

        rhs = np.empty(dimension, dtype=np.float64)
        rhs[0] = sum_y
        rhs[1:1+p] = (xy - center * my) / scale
        rhs[1+p:] = sum_y - my

        penalty = np.eye(dimension, dtype=np.float64) * self.ridge
        penalty[0, 0] = 0.0
        regularized = gram + penalty
        try:
            beta = np.linalg.solve(regularized, rhs)
        except np.linalg.LinAlgError as exc:
            raise ValueError("streaming ridge normal equations singular") from exc

        self.center = {name: float(center[j]) for j, name in enumerate(self.names)}
        self.scale = {name: float(scale[j]) for j, name in enumerate(self.names)}
        self.beta = beta
        self.rows = int(n)
        self.design_dimension = int(dimension)
        self.gram_bytes = int(gram.nbytes)
        self.maximum_batch_bytes = int(
            self.batch_size * p * 2 * np.dtype(np.float64).itemsize)
        self.target_mean = float(sum_y / n)
        variance = max(0.0, float(sum_y2 / n) - self.target_mean ** 2)
        self.target_std = float(math.sqrt(variance))
        self.condition_number = float(np.linalg.cond(regularized))
        return self

    def row(self, record):
        values = [1.0]
        features = record["features"]
        for name in self.names:
            value = features.get(name)
            if finite(value):
                values.append(
                    (float(value) - self.center[name]) / self.scale[name])
            else:
                values.append(0.0)
        values.extend(
            0.0 if finite(features.get(name)) else 1.0
            for name in self.names)
        return values

    def predict(self, record):
        return float(sum(a * b for a, b in zip(self.beta, self.row(record))))

    def predict_many(self, records):
        if not records:
            return []
        import numpy as np
        matrix = np.asarray([self.row(record) for record in records], dtype=float)
        return [float(value) for value in matrix @ self.beta]




def _polylog_value(coefficients, q):
    constant, linear, quadratic, log_term = coefficients
    q = float(q)
    return (
        float(constant)
        + float(linear) * q
        + float(quadratic) * q * q
        + float(log_term) * math.log1p(q)
    )


def _polylog_derivative_roots(linear, quadratic, log_term):
    """Stationary points of A*q + B*q^2 + C*log(1+q), q > -1."""
    a = 2.0 * float(quadratic)
    b = float(linear) + 2.0 * float(quadratic)
    d = float(linear) + float(log_term)
    tolerance = 1e-14
    if abs(a) <= tolerance:
        if abs(b) <= tolerance:
            return []
        root = -d / b
        return [root] if root > -1.0 else []
    discriminant = b * b - 4.0 * a * d
    if discriminant < -1e-12:
        return []
    discriminant = max(0.0, discriminant)
    root = math.sqrt(discriminant)
    values = [(-b - root) / (2.0 * a), (-b + root) / (2.0 * a)]
    return sorted({float(value) for value in values if value > -1.0 and finite(value)})


def _bisect_monotone_crossing(coefficients, level, left, right):
    """Unique crossing on an interval known to be monotone."""
    def value(q):
        return _polylog_value(coefficients, q) - float(level)

    fl, fr = value(left), value(right)
    if abs(fl) <= 1e-12:
        return float(left)
    if abs(fr) <= 1e-12:
        return float(right)
    if fl * fr > 0:
        return None
    lo, hi = float(left), float(right)
    for _ in range(64):
        mid = (lo + hi) / 2.0
        fm = value(mid)
        if abs(fm) <= 1e-12:
            return mid
        if fl * fm <= 0:
            hi = mid
            fr = fm
        else:
            lo = mid
            fl = fm
    return (lo + hi) / 2.0


def _polylog_level_crossings(coefficients, level, lower, upper):
    """All level crossings by partitioning at derivative roots."""
    _, linear, quadratic, log_term = coefficients
    points = [float(lower)]
    points.extend(
        root for root in _polylog_derivative_roots(linear, quadratic, log_term)
        if lower < root < upper
    )
    points.append(float(upper))
    points = sorted(set(points))
    crossings = []
    for left, right in zip(points[:-1], points[1:]):
        crossing = _bisect_monotone_crossing(
            coefficients, level, left, right)
        if crossing is not None and lower <= crossing <= upper:
            crossings.append(float(crossing))
    return sorted(set(round(value, 12) for value in crossings))



def effective_age_bucket(signal_age_ms, latency_ms):
    age = max(0.0, float(signal_age_ms)) + float(latency_ms)
    if age <= 50.0:
        return "le50"
    if age <= 100.0:
        return "50_100"
    if age <= 250.0:
        return "100_250"
    return "gt250"


def regime_support_key(row, latency_ms):
    return "::".join((
        str(row.get("asset") or "UNKNOWN"),
        str(row.get("horizon") or "UNKNOWN"),
        effective_age_bucket(
            float(row.get("signal_age_ns") or 0) / 1e6,
            int(latency_ms),
        ),
    ))


class DirectActionValueModel:
    """Direct Q(S, q, h | latency) learner with market-block calibration."""

    def __init__(
        self,
        *,
        size_grid=DEFAULT_SIZE_GRID,
        action_horizons_ms=DEFAULT_ACTION_HORIZONS_MS,
        train_latencies_ms=DEFAULT_TRAIN_LATENCIES_MS,
        entry_cap=DEFAULT_ENTRY_CAP,
        hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
        ridge=8.0,
        calibration_level=0.90,
        max_sizes_per_state=3,
        streaming_batch_size=4096,
        friction_policy=DEFAULT_FRICTION_POLICY,
        selection_calibration_mode="PREQUENTIAL",
        prequential_calibration_blocks=2,
        maximum_effective_action_age_ms=None,
        minimum_regime_action_targets=0,
        minimum_bilateral_opposite_side_markets=0,
    ):
        self.size_grid = tuple(float(v) for v in size_grid)
        self.action_horizons_ms = tuple(int(v) for v in action_horizons_ms)
        self.train_latencies_ms = tuple(int(v) for v in train_latencies_ms)
        self.entry_cap = float(entry_cap)
        self.hard_order_notional = float(hard_order_notional)
        self.ridge = float(ridge)
        self.calibration_level = float(calibration_level)
        self.max_sizes_per_state = int(max_sizes_per_state)
        self.streaming_batch_size = int(streaming_batch_size)
        self.selection_calibration_mode = str(selection_calibration_mode).upper()
        self.prequential_calibration_blocks = int(prequential_calibration_blocks)
        self.maximum_effective_action_age_ms = (
            None
            if maximum_effective_action_age_ms is None
            else float(maximum_effective_action_age_ms)
        )
        self.minimum_regime_action_targets = int(
            minimum_regime_action_targets)
        self.minimum_bilateral_opposite_side_markets = int(
            minimum_bilateral_opposite_side_markets)
        self.bilateral_support_markets = 0
        if (
            self.maximum_effective_action_age_ms is not None
            and (
                not finite(self.maximum_effective_action_age_ms)
                or self.maximum_effective_action_age_ms <= 0
            )
        ):
            raise ValueError(
                "positive finite maximum effective action age required")
        if self.minimum_regime_action_targets < 0:
            raise ValueError("nonnegative regime support threshold required")
        if self.minimum_bilateral_opposite_side_markets < 0:
            raise ValueError(
                "minimum bilateral opposite-side markets must be nonnegative")
        if self.max_sizes_per_state <= 0 or self.streaming_batch_size <= 0:
            raise ValueError("positive direct-action capacity limits required")
        if self.selection_calibration_mode not in ("PREQUENTIAL", "OFF"):
            raise ValueError("selection calibration mode must be PREQUENTIAL or OFF")
        if self.prequential_calibration_blocks <= 0:
            raise ValueError("positive prequential calibration block count required")
        self.friction_policy = friction_policy.validated()
        self.fitted = False

    def _base_feature_names(self, rows):
        available = feature_names(rows)
        priority = [
            "external.binance_return_100ms_bp",
            "external.coinbase_return_100ms_bp",
            "external.bybit_return_100ms_bp",
            "binance_return_100ms_bp",
            "coinbase_return_100ms_bp",
            "bybit_return_100ms_bp",
            "signal_return_bp",
            "signal_age_ns",
            "tte_ns",
            "bid_e4",
            "ask_e4",
            "ask_quantity",
        ]
        chosen = [name for name in priority if name in available]
        chosen.extend(name for name in available if name not in chosen)
        return tuple(chosen[:12])

    def _configure_levels(self, rows):
        self.base_names = self._base_feature_names(rows)
        self.assets = tuple(sorted({str(row.get("asset") or "UNKNOWN") for row in rows}))
        self.contract_horizons = tuple(sorted({str(row.get("horizon") or "UNKNOWN") for row in rows}))
        self.age_buckets = ("le50", "50_100", "100_250", "gt250")
        names = [
            "state.ask", "state.bid", "state.spread", "state.depth",
            "state.minimum", "state.tte_s", "state.signal_age_ms",
            "state.direction", "action.side_sign", "action.signal_alignment",
            "action.size", "action.size2",
            "action.log_size", "action.depth_fraction", "action.notional",
            "action.notional_fraction_of_cap", "action.exit_horizon_ms",
            "action.log_exit_horizon", "system.latency_ms",
            "system.log_latency", "system.effective_action_age_ms",
            "system.log_effective_action_age",
            "interaction.size_signal", "interaction.size_abs_signal",
            "interaction.size_spread", "interaction.size_signal_alignment",
            "interaction.size2_over_depth", "interaction.horizon_signal",
            "interaction.horizon_abs_signal", "interaction.size_effective_age",
            "interaction.signal_effective_age",
            "interaction.horizon_effective_age",
            "age::le50", "age::50_100", "age::100_250", "age::gt250",
        ]
        names.extend("x." + name for name in self.base_names)
        names.extend("asset::" + asset for asset in self.assets)
        names.extend("contract::" + horizon for horizon in self.contract_horizons)
        names.extend(
            "asset_contract::" + asset + "::" + horizon
            for asset in self.assets
            for horizon in self.contract_horizons
        )
        names.extend(
            "asset_contract_signal::" + asset + "::" + horizon
            for asset in self.assets
            for horizon in self.contract_horizons
        )
        names.extend(
            "asset_contract_size::" + asset + "::" + horizon
            for asset in self.assets
            for horizon in self.contract_horizons
        )
        names.extend(
            "asset_contract_age::" + asset + "::" + horizon + "::" + bucket
            for asset in self.assets
            for horizon in self.contract_horizons
            for bucket in self.age_buckets
        )
        names.extend("exit::" + str(h) for h in self.action_horizons_ms)
        names.extend("latency::" + str(v) for v in self.train_latencies_ms)
        self.model_feature_names = tuple(names)

    def _action_record(self, row, *, size, horizon_ms, latency_ms, side=None):
        side = side or selected_action_side(row)
        side_state = decision_side_state(row, side)
        if side_state is None:
            raise ValueError("action side lacks decision L1 evidence")
        ask = float(side_state["ask"])
        bid = float(side_state["bid"])
        depth = max(1e-12, float(side_state["ask_quantity"]))
        side_sign = action_side_sign(row, side)
        alignment = side_sign * float(row.get("direction") or 0)
        signal_age_ms = max(
            0.0, float(row.get("signal_age_ns") or 0) / 1e6)
        effective_action_age_ms = signal_age_ms + float(latency_ms)
        signal = 0.0
        for key in (
            "external.binance_return_100ms_bp", "binance_return_100ms_bp",
            "signal_return_bp",
        ):
            value = row.get("features", {}).get(key)
            if finite(value):
                signal = float(value)
                break
        features = {
            "state.ask": ask,
            "state.bid": bid,
            "state.spread": ask - bid,
            "state.depth": depth,
            "state.minimum": float(row["minimum"]),
            "state.tte_s": float(row["tte_ns"]) / 1e9,
            "state.signal_age_ms": signal_age_ms,
            "state.direction": float(row.get("direction") or 0),
            "action.side_sign": side_sign,
            "action.signal_alignment": alignment,
            "action.size": float(size),
            "action.size2": float(size) ** 2,
            "action.log_size": math.log1p(float(size)),
            "action.depth_fraction": float(size) / depth,
            "action.notional": float(size) * ask,
            "action.notional_fraction_of_cap": float(size) * ask / self.hard_order_notional,
            "action.exit_horizon_ms": float(horizon_ms),
            "action.log_exit_horizon": math.log1p(float(horizon_ms)),
            "system.latency_ms": float(latency_ms),
            "system.log_latency": math.log1p(float(latency_ms)),
            "system.effective_action_age_ms": effective_action_age_ms,
            "system.log_effective_action_age": math.log1p(
                effective_action_age_ms),
            "interaction.size_signal": float(size) * signal,
            "interaction.size_abs_signal": float(size) * abs(signal),
            "interaction.size_spread": float(size) * (ask - bid),
            "interaction.size_signal_alignment": float(size) * signal * alignment,
            "interaction.size2_over_depth": float(size) ** 2 / depth,
            "interaction.horizon_signal": math.log1p(float(horizon_ms)) * signal,
            "interaction.horizon_abs_signal": math.log1p(float(horizon_ms)) * abs(signal),
            "interaction.size_effective_age": float(size) * effective_action_age_ms,
            "interaction.signal_effective_age": signal * effective_action_age_ms,
            "interaction.horizon_effective_age": (
                math.log1p(float(horizon_ms)) * effective_action_age_ms),
            "age::le50": 1.0 if effective_action_age_ms <= 50.0 else 0.0,
            "age::50_100": (
                1.0 if 50.0 < effective_action_age_ms <= 100.0 else 0.0),
            "age::100_250": (
                1.0 if 100.0 < effective_action_age_ms <= 250.0 else 0.0),
            "age::gt250": 1.0 if effective_action_age_ms > 250.0 else 0.0,
        }
        source = row.get("features", {})
        for name in self.base_names:
            value = source.get(name)
            if finite(value):
                features["x." + name] = float(value)
        asset = str(row.get("asset") or "UNKNOWN")
        contract = str(row.get("horizon") or "UNKNOWN")
        bucket = effective_age_bucket(signal_age_ms, latency_ms)
        for value in self.assets:
            features["asset::" + value] = 1.0 if value == asset else 0.0
        for value in self.contract_horizons:
            features["contract::" + value] = 1.0 if value == contract else 0.0
        for asset_value in self.assets:
            for horizon_value in self.contract_horizons:
                active = (
                    1.0
                    if asset_value == asset and horizon_value == contract
                    else 0.0
                )
                prefix = (
                    asset_value + "::" + horizon_value)
                features["asset_contract::" + prefix] = active
                features["asset_contract_signal::" + prefix] = (
                    signal * active)
                features["asset_contract_size::" + prefix] = (
                    float(size) * active)
                for bucket_value in self.age_buckets:
                    features[
                        "asset_contract_age::"
                        + prefix + "::" + bucket_value
                    ] = (
                        1.0
                        if active and bucket_value == bucket else 0.0
                    )
        for value in self.action_horizons_ms:
            features["exit::" + str(value)] = 1.0 if value == int(horizon_ms) else 0.0
        for value in self.train_latencies_ms:
            features["latency::" + str(value)] = 1.0 if value == int(latency_ms) else 0.0
        return {
            "features": features,
            "market_id": str(row["market_id"]),
            "asset": asset,
            "contract_horizon": contract,
            "decision_ns": int(row["decision_ns"]),
            "side": side,
            "size": float(size),
            "exit_horizon_ms": int(horizon_ms),
            "latency_ms": int(latency_ms),
            "signal_age_ms": float(signal_age_ms),
            "effective_action_age_ms": float(effective_action_age_ms),
        }

    def _iter_training_actions(
        self, rows, *, markets=None, state_counter=None, regime_counter=None,
    ):
        market_filter = set(markets) if markets is not None else None
        for row in rows:
            if market_filter is not None and str(row["market_id"]) not in market_filter:
                continue
            if not _valid_state(row):
                if state_counter is not None:
                    state_counter["STATE_OUTSIDE_RESEARCH_SUPPORT"] += 1
                continue

            # Quantity feasibility is independent of exit horizon.  Compute it
            # once per causal state/side instead of once per horizon.
            side_sizes = {}
            for side in decision_action_sides(row):
                sizes = candidate_sizes(
                    row, side=side, size_grid=self.size_grid,
                    hard_order_notional=self.hard_order_notional,
                    max_sizes=self.max_sizes_per_state,
                )
                if sizes:
                    side_sizes[side] = sizes
                elif state_counter is not None:
                    state_counter["NO_FEASIBLE_SIZE"] += 1

            for latency in self.train_latencies_ms:
                for side, sizes in side_sizes.items():
                    for horizon in self.action_horizons_ms:
                        if horizon <= latency:
                            continue
                        kernel, kernel_state = action_execution_kernel(
                            row,
                            horizon_ms=horizon,
                            latency_ms=latency,
                            side=side,
                            entry_cap=self.entry_cap,
                        )
                        if kernel is None:
                            if state_counter is not None:
                                state_counter[kernel_state] += len(sizes)
                            continue
                        for size in sizes:
                            economics, state = economics_from_execution_kernel(
                                kernel, size)
                            if state_counter is not None:
                                state_counter[state] += 1
                            target = float(economics["cash_pnl"])
                            action = self._action_record(
                                row, size=size, horizon_ms=horizon,
                                latency_ms=latency, side=side)
                            action["target"] = target
                            action["target_state"] = state
                            if state_counter is not None:
                                state_counter["ACTION_SIDE_" + side] += 1
                            if regime_counter is not None:
                                regime_counter[
                                    regime_support_key(row, latency)
                                ] += 1
                            yield action


    def _bilateral_support_summary(self, rows):
        bilateral = set()
        yes_supported = set()
        no_supported = set()
        for row in rows:
            market = str(row.get("market_id") or "")
            if not market:
                continue
            sides = decision_action_sides(row)
            if "YES" not in sides or "NO" not in sides:
                continue
            readiness = {}
            for side in ("YES", "NO"):
                ready = False
                for latency in self.train_latencies_ms:
                    for horizon in self.action_horizons_ms:
                        if horizon <= latency:
                            continue
                        kernel, _ = action_execution_kernel(
                            row,
                            horizon_ms=horizon,
                            latency_ms=latency,
                            side=side,
                            entry_cap=self.entry_cap,
                        )
                        if kernel is not None:
                            ready = True
                            break
                    if ready:
                        break
                readiness[side] = ready
            if readiness["YES"]:
                yes_supported.add(market)
            if readiness["NO"]:
                no_supported.add(market)
            if readiness["YES"] and readiness["NO"]:
                bilateral.add(market)
        return {
            "bilateral_markets": len(bilateral),
            "yes_supported_markets": len(yes_supported),
            "no_supported_markets": len(no_supported),
            "minimum_required_markets": (
                self.minimum_bilateral_opposite_side_markets),
            "opposite_side_ready": (
                len(bilateral)
                >= self.minimum_bilateral_opposite_side_markets
            ),
            "semantics": (
                "BOTH_SIDES_REQUIRE_CAUSAL_DECISION_ARRIVAL_EXIT_SUPPORT_"
                "IN_THE_SAME_MEAN_FIT_TRAINING_MARKET"
            ),
        }

    def _eligible_action_sides(self, row):
        sides = decision_action_sides(row)
        if not ("YES" in sides and "NO" in sides):
            return sides
        if (
            int(getattr(self, "bilateral_support_markets", 0))
            >= self.minimum_bilateral_opposite_side_markets
        ):
            return sides
        return (selected_action_side(row),)

    @staticmethod
    def _market_order(rows):
        first = {}
        for row in rows:
            if not _valid_state(row):
                continue
            market = str(row["market_id"])
            first[market] = min(
                first.get(market, int(row["decision_ns"])),
                int(row["decision_ns"]))
        return sorted(first, key=lambda market: (first[market], market))


    def _prequential_clone(self):
        return DirectActionValueModel(
            size_grid=self.size_grid,
            action_horizons_ms=self.action_horizons_ms,
            train_latencies_ms=self.train_latencies_ms,
            entry_cap=self.entry_cap,
            hard_order_notional=self.hard_order_notional,
            ridge=self.ridge,
            calibration_level=self.calibration_level,
            max_sizes_per_state=self.max_sizes_per_state,
            streaming_batch_size=self.streaming_batch_size,
            friction_policy=self.friction_policy,
            selection_calibration_mode="OFF",
            prequential_calibration_blocks=self.prequential_calibration_blocks,
            maximum_effective_action_age_ms=self.maximum_effective_action_age_ms,
            minimum_regime_action_targets=self.minimum_regime_action_targets,
            minimum_bilateral_opposite_side_markets=(
                self.minimum_bilateral_opposite_side_markets),
        )

    def _prequential_selected_policy_calibration(
        self, rows, chronological_markets,
    ):
        """Aggregate honest post-selection errors from rolling OOS blocks.

        Each evaluation block is scored by a model fit only on earlier markets.
        The final deployment mean may later train on these historical blocks, so
        this is explicitly prequential calibration, not a conformal coverage
        claim for the final model.
        """
        markets = list(chronological_markets)
        minimum_warmup = 15
        if len(markets) < minimum_warmup + self.prequential_calibration_blocks:
            return {
                "state": "INSUFFICIENT_PREQUENTIAL_SELECTION_CALIBRATION",
                "penalty": 0.0,
                "selected_markets": 0,
                "observed_selected_markets": 0,
                "censored_selected_markets": 0,
                "observed_fraction": 0.0,
                "score_markets": 0,
                "blocks": [],
                "failed_blocks": [],
                "calibration_level": self.calibration_level,
                "minimum_observed_markets": 10,
                "minimum_observed_fraction": 0.50,
            }

        warmup_end = max(
            minimum_warmup,
            int(len(markets) * 0.50),
        )
        warmup_end = min(
            warmup_end,
            len(markets) - self.prequential_calibration_blocks,
        )
        remaining = len(markets) - warmup_end
        blocks = max(1, min(self.prequential_calibration_blocks, remaining))
        cutpoints = [
            warmup_end + (remaining * index // blocks)
            for index in range(blocks + 1)
        ]

        all_scores = []
        selected = observed = censored = 0
        predicted_sum = realized_sum = 0.0
        receipts = []
        failed = []

        for block_index in range(blocks):
            start, end = cutpoints[block_index], cutpoints[block_index + 1]
            eval_markets = markets[start:end]
            if not eval_markets:
                continue
            train_markets = set(markets[:start])
            train_rows = [
                row for row in rows
                if str(row.get("market_id")) in train_markets
            ]
            try:
                clone = self._prequential_clone().fit(train_rows)
            except ValueError as exc:
                failed.append({
                    "block": block_index + 1,
                    "reason": str(exc),
                    "train_markets": len(train_markets),
                    "evaluation_markets": len(eval_markets),
                })
                continue

            calibration = clone._calibrate_selected_policy(
                rows,
                set(eval_markets),
                include_score_values=True,
            )
            scores = list(calibration.get("score_values") or [])
            all_scores.extend(scores)
            selected += int(calibration.get("selected_markets") or 0)
            block_observed = int(
                calibration.get("observed_selected_markets") or 0)
            observed += block_observed
            censored += int(
                calibration.get("censored_selected_markets") or 0)
            predicted = calibration.get("predicted_lower_cash_mean")
            realized = calibration.get("realized_cash_mean")
            if block_observed and predicted is not None and realized is not None:
                predicted_sum += float(predicted) * block_observed
                realized_sum += float(realized) * block_observed
            receipts.append({
                "block": block_index + 1,
                "train_markets": len(train_markets),
                "evaluation_markets": len(eval_markets),
                "selected_markets": int(
                    calibration.get("selected_markets") or 0),
                "observed_selected_markets": block_observed,
                "censored_selected_markets": int(
                    calibration.get("censored_selected_markets") or 0),
                "score_markets": len(scores),
                "clone_calibration_state": clone.training_receipt.get(
                    "calibration_state"),
                "clone_calibration_multiplier": clone.training_receipt.get(
                    "calibration_multiplier"),
            })

        observed_fraction = observed / selected if selected else 0.0
        penalty = _quantile(all_scores, self.calibration_level)
        ready = (
            penalty is not None
            and finite(penalty)
            and observed >= 10
            and observed_fraction >= 0.50
        )
        return {
            "state": (
                "PREQUENTIAL_SELECTED_POLICY_ONE_SIDED"
                if ready
                else "INSUFFICIENT_PREQUENTIAL_SELECTION_CALIBRATION"
            ),
            "penalty": float(max(0.0, penalty)) if ready else 0.0,
            "selected_markets": selected,
            "observed_selected_markets": observed,
            "censored_selected_markets": censored,
            "observed_fraction": observed_fraction,
            "score_markets": len(all_scores),
            "blocks": receipts,
            "failed_blocks": failed,
            "predicted_lower_cash_mean": (
                predicted_sum / observed if observed else None
            ),
            "realized_cash_mean": (
                realized_sum / observed if observed else None
            ),
            "mean_optimism_after_action_conformal": (
                (predicted_sum - realized_sum) / observed
                if observed else None
            ),
            "calibration_level": self.calibration_level,
            "minimum_observed_markets": 10,
            "minimum_observed_fraction": 0.50,
            "semantics": (
                "ROLLING_MARKET_BLOCK_OOS_SCORES;"
                "FINAL_MODEL_MAY_LATER_REFIT_ON_HISTORICAL_SCORE_BLOCKS;"
                "NOT_A_FINAL_MODEL_CONFORMAL_COVERAGE_CLAIM"
            ),
        }

    def _calibrate_selected_policy(
        self, rows, calibration_markets, *, include_score_values=False,
    ):
        """Calibrate optimism after the continuous action argmax.

        This holdout is disjoint from mean fitting and from the action-level
        conformal holdout. One selected action per market is retained.
        """
        market_filter = set(calibration_markets)
        ordered = sorted(
            (
                row for row in rows
                if str(row.get("market_id")) in market_filter
            ),
            key=lambda row: (row["decision_ns"], row["decision_id"]),
        )
        used_markets = set()
        scores = {}
        selected = observed = censored = 0
        predicted_sum = realized_sum = 0.0
        prior_penalty = float(getattr(self, "selection_optimism_penalty", 0.0))
        prior_fitted = bool(self.fitted)
        self.selection_optimism_penalty = 0.0
        self.fitted = True
        try:
            for row in ordered:
                market = str(row["market_id"])
                if market in used_markets:
                    continue
                best = None
                for latency_ms in self.train_latencies_ms:
                    ranked, _ = self.score_actions(
                        row,
                        latency_ms=latency_ms,
                        available_capital=None,
                        live_geometry=True,
                        portfolio_state={},
                        capital_budget=10_000.0,
                    )
                    if not ranked:
                        continue
                    candidate = ranked[0]
                    if candidate["calibrated_lower_value"] <= 0:
                        continue
                    if (
                        best is None
                        or candidate["calibrated_lower_value"]
                        > best["calibrated_lower_value"]
                    ):
                        best = candidate
                if best is None:
                    continue

                # Current policy allows at most one entry per market.
                used_markets.add(market)
                selected += 1
                economics, _ = realized_action_economics(
                    row,
                    size=best["size"],
                    horizon_ms=best["exit_horizon_ms"],
                    latency_ms=best["latency_ms"],
                    side=best.get("side"),
                    entry_cap=self.entry_cap,
                    hard_order_notional=self.hard_order_notional,
                )
                if economics is None:
                    censored += 1
                    continue

                observed += 1
                realized = float(economics["cash_pnl"])
                predicted_lower = float(best["calibrated_lower_cash_value"])
                scores[market] = max(0.0, predicted_lower - realized)
                predicted_sum += predicted_lower
                realized_sum += realized
        finally:
            self.fitted = prior_fitted
            self.selection_optimism_penalty = prior_penalty

        observed_fraction = observed / selected if selected else 0.0
        penalty = _quantile(list(scores.values()), self.calibration_level)
        ready = (
            penalty is not None
            and finite(penalty)
            and observed >= 10
            and observed_fraction >= 0.50
        )
        return {
            "state": (
                "TEMPORAL_MARKET_BLOCK_POST_ARGMAX_ONE_SIDED"
                if ready
                else "INSUFFICIENT_POST_ARGMAX_CALIBRATION"
            ),
            "penalty": float(max(0.0, penalty)) if ready else 0.0,
            "selected_markets": selected,
            "observed_selected_markets": observed,
            "censored_selected_markets": censored,
            "observed_fraction": observed_fraction,
            "score_markets": len(scores),
            "predicted_lower_cash_mean": (
                predicted_sum / observed if observed else None
            ),
            "realized_cash_mean": (
                realized_sum / observed if observed else None
            ),
            "mean_optimism_after_action_conformal": (
                (predicted_sum - realized_sum) / observed
                if observed else None
            ),
            "calibration_level": self.calibration_level,
            "minimum_observed_markets": 10,
            "minimum_observed_fraction": 0.50,
            **({"score_values": sorted(scores.values())}
               if include_score_values else {}),
        }

    def fit(self, rows):
        rows = list(rows)
        if not rows:
            raise ValueError("direct action training rows required")
        self._configure_levels(rows)
        markets = self._market_order(rows)
        if not markets:
            raise ValueError("no admissible direct-action training markets")

        def factory(
            market_subset=None, counter=None, regime_counter=None,
        ):
            return lambda: self._iter_training_actions(
                rows,
                markets=market_subset,
                state_counter=counter,
                regime_counter=regime_counter,
            )

        self.uncertainty_floor = 1e-6
        self.calibration_multiplier = 1.5
        self.selection_optimism_penalty = 0.0
        self.selection_calibration = {
            "state": (
                "DISABLED"
                if self.selection_calibration_mode == "OFF"
                else "INSUFFICIENT_PREQUENTIAL_SELECTION_CALIBRATION"
            ),
            "penalty": 0.0,
            "selected_markets": 0,
            "observed_selected_markets": 0,
            "censored_selected_markets": 0,
            "observed_fraction": 0.0,
            "score_markets": 0,
            "predicted_lower_cash_mean": None,
            "realized_cash_mean": None,
            "mean_optimism_after_action_conformal": None,
            "calibration_level": self.calibration_level,
            "minimum_observed_markets": 10,
            "minimum_observed_fraction": 0.50,
        }
        self.scale_model = None
        calibration_state = "INSUFFICIENT_MARKET_BLOCKS"
        calibration_block_scores = 0

        # Chronological split:
        # 60% mean seed, 20% residual scale, final 20% held out from mean for
        # action-level split conformal. Post-argmax optimism is estimated
        # prequentially on rolling OOS blocks inside the earlier 80%.
        fit_markets = scale_markets = calibration_markets = set()
        mean_fit_markets = set(markets)
        mean_fit_sequence = list(markets)
        if len(markets) >= 15:
            fit_end = max(1, int(len(markets) * 0.60))
            scale_end = max(fit_end + 1, int(len(markets) * 0.80))
            scale_end = min(scale_end, len(markets) - 1)
            fit_markets = set(markets[:fit_end])
            scale_markets = set(markets[fit_end:scale_end])
            calibration_markets = set(markets[scale_end:])
            mean_fit_sequence = list(markets[:scale_end])

        target_states = Counter()
        regime_action_targets = Counter()
        if fit_markets and scale_markets and calibration_markets:
            provisional = StreamingRidge(
                self.model_feature_names, ridge=self.ridge,
                batch_size=self.streaming_batch_size).fit_factory(
                    factory(fit_markets), lambda action: action["target"])

            self.scale_model = StreamingRidge(
                self.model_feature_names, ridge=self.ridge,
                batch_size=self.streaming_batch_size).fit_factory(
                    factory(scale_markets),
                    lambda action: abs(
                        float(action["target"]) - provisional.predict(action)),
                )
            self.uncertainty_floor = max(
                1e-6, 0.10 * self.scale_model.target_mean)

            mean_fit_markets = fit_markets | scale_markets
            deployment_mean = StreamingRidge(
                self.model_feature_names, ridge=self.ridge,
                batch_size=self.streaming_batch_size).fit_factory(
                    factory(
                        mean_fit_markets,
                        target_states,
                        regime_action_targets,
                    ),
                    lambda action: action["target"])

            # Action-level split conformal uses the full untouched final 20%.
            block_scores = {}
            for action in factory(calibration_markets)():
                predicted = deployment_mean.predict(action)
                scale = max(
                    self.uncertainty_floor,
                    self.scale_model.predict(action))
                score = abs(float(action["target"]) - predicted) / scale
                market = str(action["market_id"])
                block_scores[market] = max(
                    float(score), block_scores.get(market, 0.0))
            calibration_block_scores = len(block_scores)
            calibrated = _quantile(
                list(block_scores.values()), self.calibration_level)
            if calibrated is not None and finite(calibrated):
                self.calibration_multiplier = max(1.0, float(calibrated))
                calibration_state = "TEMPORAL_MARKET_BLOCK_SPLIT_CONFORMAL"
            else:
                calibration_state = "INSUFFICIENT_CALIBRATION_ACTION_TARGETS"

            # Freeze fitted objects before prequential policy calibration.
            self.mean_model = deployment_mean
            self.fitted = True
            if self.selection_calibration_mode == "PREQUENTIAL":
                self.selection_calibration = (
                    self._prequential_selected_policy_calibration(
                        rows, mean_fit_sequence)
                )
                self.selection_optimism_penalty = float(
                    self.selection_calibration["penalty"])
        else:
            deployment_mean = StreamingRidge(
                self.model_feature_names, ridge=self.ridge,
                batch_size=self.streaming_batch_size).fit_factory(
                    factory(None, target_states, regime_action_targets),
                    lambda action: action["target"])

        if self.scale_model is None:
            self.uncertainty_floor = max(
                1e-6, deployment_mean.target_std)

        self.mean_model = deployment_mean
        support_rows = [
            row for row in rows
            if str(row.get("market_id")) in mean_fit_markets
        ]
        bilateral_support = self._bilateral_support_summary(support_rows)
        self.bilateral_support_markets = int(
            bilateral_support["bilateral_markets"])
        self.regime_action_target_counts = dict(regime_action_targets)
        training_states_used = sum(
            1 for row in rows if str(row["market_id"]) in mean_fit_markets)
        self.training_receipt = {
            "schema": SCHEMA + "_training_v1",
            **SAFETY,
            "state": "READY",
            "training_states_total": len(rows),
            "training_states_used": training_states_used,
            "training_markets_total": len({str(row["market_id"]) for row in rows}),
            "training_markets_used": len(mean_fit_markets),
            "training_state_cap": None,
            "action_targets": deployment_mean.rows,
            "target_state_counts": dict(target_states),
            "size_grid": list(self.size_grid),
            "action_horizons_ms": list(self.action_horizons_ms),
            "train_latencies_ms": list(self.train_latencies_ms),
            "latency_role": "CONDITIONING_STATE_NOT_OPTIMIZED_ACTION",
            "effective_action_age_semantics": (
                "DECISION_SIGNAL_AGE_PLUS_MODELED_EXECUTION_LATENCY"),
            "latency_decay_training_support_ms": list(
                self.train_latencies_ms),
            "maximum_effective_action_age_ms": (
                None if self.maximum_effective_action_age_ms is None
                else float(self.maximum_effective_action_age_ms)
            ),
            "partial_pooling": (
                "GLOBAL_BASE_PLUS_RIDGE_SHRUNK_ASSET_CONTRACT_AGE_DEVIATIONS"
            ),
            "minimum_regime_action_targets": (
                self.minimum_regime_action_targets),
            "regime_action_target_counts": dict(
                sorted(self.regime_action_target_counts.items())),
            "bilateral_side_support": bilateral_support,
            "action_space": ["NO_TRADE", "YES_X_SIZE_X_EXIT_HORIZON", "NO_X_SIZE_X_EXIT_HORIZON"],
            "opposite_side_counterfactual": (
                "AVAILABLE_ONLY_WITH_CAUSAL_BILATERAL_L1_DECISION_ARRIVAL_"
                "EXIT_EVIDENCE_AND_MEAN_FIT_TRAINING_SUPPORT_GATE"
            ),
            "entry_cap": self.entry_cap,
            "hard_order_notional": self.hard_order_notional,
            "model": "STREAMING_RIDGE_DIRECT_EXECUTABLE_CASH_PNL",
            "matrix_strategy": "ONE_PASS_SUFFICIENT_STATISTICS_THEN_P_X_P_NORMAL_EQUATIONS",
            "design_dimension": deployment_mean.design_dimension,
            "gram_matrix_bytes": deployment_mean.gram_bytes,
            "maximum_streaming_batch_bytes": deployment_mean.maximum_batch_bytes,
            "regularized_gram_condition_number": deployment_mean.condition_number,
            "streaming_batch_size": self.streaming_batch_size,
            "policy_objective": "PREDICTED_EXECUTABLE_CASH_PNL_MINUS_ACTION_UNCERTAINTY_MINUS_POST_ARGMAX_OPTIMISM_MINUS_RESIDUAL_PORTFOLIO_FRICTIONS",
            "policy_loss": "NEGATIVE_POLICY_UTILITY_WITH_NO_TRADE_BASELINE_ZERO",
            "execution_frictions_in_training_target": [
                "decision_spread_via_executable_entry",
                "post_signal_latency_price_drift_via_arrival_book",
                "fill_and_no_fill",
                "partial_fill",
                "exit_spread_via_executable_bid",
                "exit_l1_capacity_and_zero_value_residual_lower_bound",
                "entry_taker_fee",
                "exit_taker_fee",
                "post_fill_adverse_repricing",
            ],
            "residual_policy_frictions": {
                "capital_charge_bps_per_second": self.friction_policy.capital_charge_bps_per_second,
                "asset_concentration_lambda": self.friction_policy.asset_concentration_lambda,
                "common_factor_concentration_lambda": self.friction_policy.common_factor_concentration_lambda,
                "uncertainty_aversion": self.friction_policy.uncertainty_aversion,
            },
            "ridge": self.ridge,
            "uncertainty": "STREAMING_ABSOLUTE_RESIDUAL_SCALE_WITH_FINAL20_ACTION_CONFORMAL_AND_PREQUENTIAL_POST_ARGMAX_CALIBRATION",
            "calibration_level": self.calibration_level,
            "calibration_multiplier": self.calibration_multiplier,
            "uncertainty_floor": self.uncertainty_floor,
            "calibration_state": calibration_state,
            "selection_optimism_penalty": self.selection_optimism_penalty,
            "selection_calibration": dict(self.selection_calibration),
            "selection_calibration_state": self.selection_calibration["state"],
            "mean_fit_scope": (
                "PRE_CALIBRATION_MARKETS_ONLY"
                if calibration_markets else "ALL_MARKETS_SMALL_SAMPLE_FALLBACK"
            ),
            "fit_market_count": len(fit_markets),
            "scale_market_count": len(scale_markets),
            "calibration_market_count": len(calibration_markets),
            "action_calibration_market_count": len(calibration_markets),
            "selection_calibration_mode": self.selection_calibration_mode,
            "prequential_calibration_blocks": self.prequential_calibration_blocks,
            "selection_prequential_score_markets": int(
                self.selection_calibration.get("score_markets") or 0),
            "selection_prequential_observed_markets": int(
                self.selection_calibration.get(
                    "observed_selected_markets") or 0),
            "calibration_block_scores": calibration_block_scores,
            "calibration_holdout_excluded_from_mean_fit": bool(
                calibration_markets),
            "calibration_holdouts_disjoint": True,
            "selection_scores_oos_when_generated": True,
            "selection_score_blocks_may_enter_final_mean_fit": (
                self.selection_calibration_mode == "PREQUENTIAL"),
            "calibration_semantics": (
                "MEAN_FIT_FIRST_80_PERCENT;"
                "ACTION_CONFORMAL_FINAL_20_PERCENT;"
                "POST_ARGMAX_OPTIMISM_PREQUENTIAL_ROLLING_OOS_WITHIN_FIRST_80"
            ),
            "feature_names": list(self.model_feature_names),
            "capacity_scope": "L1_ONLY_NO_COUNTERFACTUAL_IMPACT_BEYOND_VISIBLE_DEPTH",
            "mean_covariance_estimation": False,
        }
        self.fitted = True
        return self

    def _signal_scalar(self, row):
        for key in (
            "external.binance_return_100ms_bp", "binance_return_100ms_bp",
            "signal_return_bp",
        ):
            value = row.get("features", {}).get(key)
            if finite(value):
                return float(value)
        return 0.0

    @staticmethod
    def _raw_feature_coefficient(model, name):
        try:
            index = model.names.index(name)
        except ValueError:
            return 0.0
        return float(model.beta[1 + index]) / float(model.scale[name])

    def _quantity_shape(self, model, row, *, horizon_ms, latency_ms, side):
        """Represent model prediction as c + A*q + B*q^2 + C*log(1+q)."""
        base = self._action_record(
            row, size=0.0, horizon_ms=horizon_ms, latency_ms=latency_ms, side=side)
        constant = float(model.predict(base))
        side_state = decision_side_state(row, side)
        if side_state is None:
            raise ValueError("quantity shape side unavailable")
        ask = float(side_state["ask"])
        bid = float(side_state["bid"])
        depth = max(1e-12, float(side_state["ask_quantity"]))
        signal = self._signal_scalar(row)
        spread = ask - bid
        side_sign = action_side_sign(row, side)
        alignment = side_sign * float(row.get("direction") or 0)

        linear = 0.0
        quadratic = 0.0
        log_term = 0.0
        linear += self._raw_feature_coefficient(model, "action.size")
        quadratic += self._raw_feature_coefficient(model, "action.size2")
        log_term += self._raw_feature_coefficient(model, "action.log_size")
        linear += (
            self._raw_feature_coefficient(model, "action.depth_fraction")
            / depth
        )
        linear += self._raw_feature_coefficient(model, "action.notional") * ask
        linear += (
            self._raw_feature_coefficient(
                model, "action.notional_fraction_of_cap")
            * ask / self.hard_order_notional
        )
        linear += (
            self._raw_feature_coefficient(model, "interaction.size_signal")
            * signal
        )
        linear += (
            self._raw_feature_coefficient(
                model, "interaction.size_abs_signal")
            * abs(signal)
        )
        linear += (
            self._raw_feature_coefficient(model, "interaction.size_spread")
            * spread
        )
        effective_action_age_ms = (
            max(0.0, float(row.get("signal_age_ns") or 0) / 1e6)
            + float(latency_ms)
        )
        linear += (
            self._raw_feature_coefficient(
                model, "interaction.size_effective_age")
            * effective_action_age_ms
        )
        asset = str(row.get("asset") or "UNKNOWN")
        contract = str(row.get("horizon") or "UNKNOWN")
        linear += self._raw_feature_coefficient(
            model,
            "asset_contract_size::" + asset + "::" + contract,
        )
        linear += (
            self._raw_feature_coefficient(
                model, "interaction.size_signal_alignment")
            * signal * alignment
        )
        quadratic += (
            self._raw_feature_coefficient(
                model, "interaction.size2_over_depth")
            / depth
        )
        return (constant, linear, quadratic, log_term)

    def _quantity_bounds(self, row, available_capital, side):
        side_state = decision_side_state(row, side)
        if side_state is None:
            return float(row["minimum"]), 0.0
        ask = float(side_state["ask"])
        lower = float(row["minimum"])
        cap = self.hard_order_notional
        if available_capital is not None:
            cap = min(cap, max(0.0, float(available_capital)))
        upper = min(float(side_state["ask_quantity"]), cap / ask)
        return lower, upper

    def _score_quantity(self, row, *, size, horizon_ms, latency_ms, side,
                        portfolio_state, capital_budget):
        action = self._action_record(
            row, size=size, horizon_ms=horizon_ms, latency_ms=latency_ms, side=side)
        mean = float(self.mean_model.predict(action))
        if self.scale_model is None:
            scale = float(self.uncertainty_floor)
        else:
            scale = max(
                float(self.uncertainty_floor),
                float(self.scale_model.predict(action)),
            )
        side_state = decision_side_state(row, side)
        notional = float(size) * float(side_state["ask"])
        uncertainty_penalty = (
            float(self.friction_policy.uncertainty_aversion)
            * self.calibration_multiplier * scale
        )
        selection_optimism_penalty = float(
            getattr(self, "selection_optimism_penalty", 0.0))
        base = {
            "action": "TRADE",
            "side": side,
            "size": float(size),
            "exit_horizon_ms": int(horizon_ms),
            "latency_ms": int(latency_ms),
            "signal_age_ms": max(
                0.0, float(row.get("signal_age_ns") or 0) / 1e6),
            "effective_action_age_ms": (
                max(0.0, float(row.get("signal_age_ns") or 0) / 1e6)
                + float(latency_ms)
            ),
            "regime_support_key": regime_support_key(row, latency_ms),
            "regime_action_target_support": int(
                getattr(self, "regime_action_target_counts", {}).get(
                    regime_support_key(row, latency_ms), 0)),
            "notional": float(notional),
        }
        residual = residual_policy_friction(
            base, row, portfolio_state=portfolio_state,
            capital_budget=capital_budget,
            friction_policy=self.friction_policy)
        lower_cash = (
            mean - uncertainty_penalty - selection_optimism_penalty)
        policy_utility = lower_cash - residual["total_residual_friction"]
        return {
            **base,
            "predicted_total_net_cash_pnl": mean,
            "predicted_total_net_pnl": mean,
            "predicted_abs_error_scale": scale,
            "uncertainty_penalty": float(uncertainty_penalty),
            "selection_optimism_penalty": float(selection_optimism_penalty),
            "calibrated_lower_cash_value": float(lower_cash),
            "calibrated_lower_value": float(policy_utility),
            "policy_utility": float(policy_utility),
            "policy_loss": float(-policy_utility),
            **residual,
            "predicted_return_on_notional": (
                mean / notional if notional > 0 else None),
            "quantity_optimizer": "GLOBAL_PIECEWISE_POLYLOG_CRITICAL_POINTS",
        }

    def _continuous_quantity_candidates(
        self, row, *, horizon_ms, latency_ms, side, lower, upper,
        portfolio_state, capital_budget,
    ):
        """Global critical-point set for the learned 1-D policy utility."""
        lower, upper = float(lower), float(upper)
        if upper + 1e-12 < lower:
            return []
        if math.isclose(lower, upper, rel_tol=0.0, abs_tol=1e-12):
            return [lower]

        mean_shape = self._quantity_shape(
            self.mean_model, row,
            horizon_ms=horizon_ms, latency_ms=latency_ms, side=side)
        scale_shape = (
            self._quantity_shape(
                self.scale_model, row,
                horizon_ms=horizon_ms, latency_ms=latency_ms, side=side)
            if self.scale_model is not None else None
        )
        uncertainty_multiplier = (
            float(self.friction_policy.uncertainty_aversion)
            * float(self.calibration_multiplier)
        )

        boundaries = {lower, upper}
        if scale_shape is not None:
            boundaries.update(
                _polylog_level_crossings(
                    scale_shape, self.uncertainty_floor, lower, upper))

        side_state = decision_side_state(row, side)
        ask = float(side_state["ask"])
        direction = action_side_sign(row, side)
        state = portfolio_state or {}
        asset_signed = state.get("asset_signed_notional") or {}
        asset = str(row.get("asset") or "UNKNOWN")
        current_asset = float(asset_signed.get(asset, 0.0) or 0.0)
        current_factor = float(state.get("common_factor_signed_notional") or 0.0)

        def add_concentration_kink(current, weight):
            if float(weight) <= 0 or ask <= 0:
                return
            root = -2.0 * float(current) * direction / ask
            if lower < root < upper:
                boundaries.add(float(root))

        add_concentration_kink(
            current_asset, self.friction_policy.asset_concentration_lambda)
        add_concentration_kink(
            current_factor,
            self.friction_policy.common_factor_concentration_lambda)

        points = sorted(boundaries)
        candidates = set(points)
        capital_linear = (
            ask * float(self.friction_policy.capital_charge_bps_per_second)
            * 1e-4 * (float(horizon_ms) / 1000.0)
        )
        budget = max(1e-12, float(capital_budget))

        for left, right in zip(points[:-1], points[1:]):
            if right - left <= 1e-12:
                continue
            midpoint = (left + right) / 2.0

            _, linear, quadratic, log_term = mean_shape

            if scale_shape is not None and (
                _polylog_value(scale_shape, midpoint)
                > self.uncertainty_floor
            ):
                _, sl, sq, sg = scale_shape
                linear -= uncertainty_multiplier * sl
                quadratic -= uncertainty_multiplier * sq
                log_term -= uncertainty_multiplier * sg

            linear -= capital_linear

            def subtract_concentration(current, weight):
                nonlocal linear, quadratic
                weight = float(weight)
                if weight <= 0:
                    return
                increment = (
                    2.0 * float(current) * direction * ask * midpoint
                    + ask * ask * midpoint * midpoint
                ) / budget
                if increment > 0:
                    linear -= (
                        weight * 2.0 * float(current) * direction * ask
                        / budget
                    )
                    quadratic -= weight * ask * ask / budget

            subtract_concentration(
                current_asset,
                self.friction_policy.asset_concentration_lambda)
            subtract_concentration(
                current_factor,
                self.friction_policy.common_factor_concentration_lambda)

            for root in _polylog_derivative_roots(
                linear, quadratic, log_term):
                if left < root < right:
                    candidates.add(float(root))

        # Numerical guard points around kinks protect against finite-precision
        # classification of active max() penalties.
        width = max(1e-10, upper - lower)
        for boundary in list(boundaries):
            for direction_eps in (-1.0, 1.0):
                value = boundary + direction_eps * width * 1e-10
                if lower <= value <= upper:
                    candidates.add(value)
        return sorted(
            value for value in candidates
            if lower - 1e-12 <= value <= upper + 1e-12)

    def score_actions(self, row, *, latency_ms=50, available_capital=None,
                      live_geometry=True, portfolio_state=None,
                      capital_budget=10_000.0):
        if not self.fitted:
            raise RuntimeError("direct action model not fitted")
        latency_ms = int(latency_ms)
        if latency_ms not in self.train_latencies_ms:
            return [], "LATENCY_OUTSIDE_TRAINING_SUPPORT"
        if not _valid_state(row):
            return [], "STATE_OUTSIDE_RESEARCH_SUPPORT"
        signal_age_ms = max(
            0.0, float(row.get("signal_age_ns") or 0) / 1e6)
        effective_action_age_ms = signal_age_ms + float(latency_ms)
        if (
            self.maximum_effective_action_age_ms is not None
            and effective_action_age_ms
            > self.maximum_effective_action_age_ms + 1e-12
        ):
            return [], "EFFECTIVE_ACTION_AGE_EXCEEDED"
        regime_key = regime_support_key(row, latency_ms)
        regime_support = int(
            getattr(self, "regime_action_target_counts", {}).get(
                regime_key, 0))
        if (
            self.minimum_regime_action_targets > 0
            and regime_support < self.minimum_regime_action_targets
        ):
            return [], "INSUFFICIENT_REGIME_SUPPORT"
        if live_geometry and not (
            LIVE_MINIMUM_TTE_NS <= int(row["tte_ns"]) <= LIVE_MAXIMUM_TTE_NS
            and any(
                (decision_side_state(row, side) is not None
                 and float(decision_side_state(row, side)["ask"]) <= self.entry_cap + 1e-12)
                for side in self._eligible_action_sides(row)
            )
        ):
            return [], "OUTSIDE_LIVE_GEOMETRY"

        scored = []
        for side in self._eligible_action_sides(row):
            lower, upper = self._quantity_bounds(row, available_capital, side)
            if upper + 1e-12 < lower or upper <= 0:
                continue
            for horizon in self.action_horizons_ms:
                if horizon <= latency_ms:
                    continue
                quantities = self._continuous_quantity_candidates(
                    row, horizon_ms=horizon, latency_ms=latency_ms, side=side,
                    lower=lower, upper=upper,
                    portfolio_state=portfolio_state,
                    capital_budget=capital_budget)
                if not quantities:
                    continue
                horizon_scores = [
                    self._score_quantity(
                        row, size=q, horizon_ms=horizon, latency_ms=latency_ms,
                        side=side, portfolio_state=portfolio_state,
                        capital_budget=capital_budget)
                    for q in quantities
                ]
                horizon_scores.sort(
                    key=lambda value: (
                        value["policy_utility"],
                        value["predicted_total_net_cash_pnl"],
                        -value["notional"],
                    ),
                    reverse=True,
                )
                best = dict(horizon_scores[0])
                best["quantity_candidate_count"] = len(horizon_scores)
                scored.append(best)

        scored.sort(
            key=lambda value: (
                value["policy_utility"],
                value["predicted_total_net_cash_pnl"],
                -value["notional"],
            ),
            reverse=True,
        )
        return scored, "READY" if scored else "NO_FEASIBLE_ACTION"

    def select_action(self, row, *, latency_ms=50, available_capital=None,
                      live_geometry=True, minimum_lower_value=0.0,
                      portfolio_state=None, capital_budget=10_000.0):
        scored, state = self.score_actions(
            row, latency_ms=latency_ms, available_capital=available_capital,
            live_geometry=live_geometry, portfolio_state=portfolio_state,
            capital_budget=capital_budget)
        if not scored or scored[0]["calibrated_lower_value"] <= float(minimum_lower_value):
            return {
                "action": "NO_TRADE",
                "reason": state if not scored else "LOWER_VALUE_NONPOSITIVE",
                "latency_ms": int(latency_ms),
                "calibrated_lower_value": 0.0,
                "predicted_total_net_pnl": 0.0,
                "policy_utility": 0.0,
                "policy_loss": 0.0,
            }
        selected = dict(scored[0])
        selected["reason"] = "DIRECT_ACTION_VALUE_MAXIMUM"
        return selected


def evaluate_direct_action_policy(
    model,
    rows,
    *,
    latency_ms=50,
    capital_budget=10_000.0,
    one_entry_per_market=True,
    live_geometry=True,
):
    """Sequential OOS portfolio replay with horizon-aware capital release."""
    ordered = sorted(rows, key=lambda row: (row["decision_ns"], row["decision_id"]))
    used_markets = set()
    active = []
    outcomes = []
    max_active_positions = 0
    max_gross_notional = 0.0

    for row in ordered:
        now_ns = int(row["decision_ns"])
        active = [position for position in active if position["release_ns"] > now_ns]
        gross_notional = sum(position["notional"] for position in active)
        asset_signed = defaultdict(float)
        common_factor_signed = 0.0
        for position in active:
            asset_signed[position["asset"]] += position["signed_notional"]
            common_factor_signed += position["signed_notional"]
        portfolio_state = {
            "active_positions": len(active),
            "gross_notional": gross_notional,
            "asset_signed_notional": dict(asset_signed),
            "common_factor_signed_notional": common_factor_signed,
        }
        max_active_positions = max(max_active_positions, len(active))
        max_gross_notional = max(max_gross_notional, gross_notional)

        market = str(row["market_id"])
        if one_entry_per_market and market in used_markets:
            outcomes.append({
                "market_id": market, "asset": row["asset"], "decision_ns": now_ns,
                "action": "NO_TRADE", "reason": "MARKET_ALREADY_TRADED",
                "realized_pnl": 0.0, "observed": True,
                "portfolio_state_before": portfolio_state,
            })
            continue

        available = max(0.0, float(capital_budget) - gross_notional)
        selected = model.select_action(
            row, latency_ms=latency_ms, available_capital=available,
            live_geometry=live_geometry, portfolio_state=portfolio_state,
            capital_budget=capital_budget)
        outcome = {
            "market_id": market,
            "asset": row["asset"],
            "contract_horizon": row["horizon"],
            "decision_ns": now_ns,
            "portfolio_state_before": portfolio_state,
            **selected,
        }
        if selected["action"] == "NO_TRADE":
            outcome.update({"realized_pnl": 0.0, "observed": True})
            outcomes.append(outcome)
            continue

        if one_entry_per_market:
            used_markets.add(market)

        selected_side = str(
            selected.get("side") or selected_action_side(row))
        side_state = decision_side_state(row, selected_side)
        if side_state is None:
            raise ValueError("selected action missing causal decision-side state")
        entry_limit = float(side_state["ask"])
        selected_size = float(selected["size"])
        entry_fee_bound = fee_per_share(row, entry_limit) * selected_size
        worst_case_loss_bound = (
            float(selected["notional"]) + float(entry_fee_bound))
        outcome["censored_worst_case_loss_bound"] = worst_case_loss_bound
        outcome["censored_worst_case_pnl"] = -worst_case_loss_bound
        outcome["censored_worst_case_semantics"] = (
            "ASSUME_FILL_AT_DECISION_LIMIT_AND_ZERO_TERMINAL_VALUE;"
            "ENTRY_FEE_BOUND_INCLUDED"
        )

        direction = action_side_sign(row, selected_side)
        active.append({
            "market_id": market,
            "asset": str(row["asset"]),
            "notional": float(selected["notional"]),
            "signed_notional": direction * float(selected["notional"]),
            "release_ns": now_ns + int(selected["exit_horizon_ms"]) * 1_000_000,
        })
        max_active_positions = max(max_active_positions, len(active))
        max_gross_notional = max(
            max_gross_notional,
            sum(position["notional"] for position in active))

        economics, target_state = realized_action_economics(
            row,
            size=selected["size"],
            horizon_ms=selected["exit_horizon_ms"],
            latency_ms=latency_ms,
            side=selected.get("side"),
            entry_cap=model.entry_cap,
            hard_order_notional=model.hard_order_notional,
        )
        outcome["target_state"] = target_state
        outcome["observed"] = economics is not None
        outcome["realized_pnl"] = (
            None if economics is None else float(economics["cash_pnl"]))
        outcome["realized_economics"] = economics
        outcomes.append(outcome)

    for row in outcomes:
        row["replay_max_active_positions"] = max_active_positions
        row["replay_max_gross_notional"] = max_gross_notional
    return outcomes


def bilateral_evidence_summary(records):
    """Aggregate support for causal YES/NO counterfactual actions."""
    decision_states = Counter()
    target_states = {
        str(horizon): Counter() for horizon in DEFAULT_ACTION_HORIZONS_MS
    }
    for row in records:
        pair = row.get("pair")
        state = (
            str(pair.get("state"))
            if isinstance(pair, dict) and pair.get("state")
            else "UNAVAILABLE"
        )
        decision_states[state] += 1
        for horizon in DEFAULT_ACTION_HORIZONS_MS:
            target = (row.get("targets") or {}).get(str(horizon), {})
            target_pair = target.get("pair") if isinstance(target, dict) else None
            target_state = (
                str(target_pair.get("state"))
                if isinstance(target_pair, dict) and target_pair.get("state")
                else "UNAVAILABLE"
            )
            target_states[str(horizon)][target_state] += 1
    return {
        "schema": SCHEMA + "_bilateral_evidence_v1",
        **SAFETY,
        "decisions": len(records),
        "decision_pair_states": dict(decision_states),
        "bilateral_ready_decision_fraction": (
            decision_states["BILATERAL_EXECUTABLE_READY"] / len(records)
            if records else None
        ),
        "target_pair_states_by_horizon_ms": {
            horizon: dict(counts) for horizon, counts in target_states.items()
        },
        "counterfactual_side_rule": (
            "YES_AND_NO_ONLY_WHEN_DECISION_ARRIVAL_AND_EXIT_HAVE_"
            "BILATERAL_EXECUTABLE_READY;OTHERWISE_SELECTED_SIDE_ONLY"
        ),
    }


def numeric_distribution(values):
    values = [float(value) for value in values if finite(value)]
    if not values:
        return {"count": 0, "min": None, "p10": None, "p25": None,
                "p50": None, "p75": None, "p90": None, "p99": None,
                "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "p10": _quantile(values, .10),
        "p25": _quantile(values, .25),
        "p50": _quantile(values, .50),
        "p75": _quantile(values, .75),
        "p90": _quantile(values, .90),
        "p99": _quantile(values, .99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def summarize_direct_action(outcomes):
    trades = [row for row in outcomes if row.get("action") == "TRADE"]
    observed = [row for row in trades if row.get("realized_pnl") is not None]
    pnl = [float(row["realized_pnl"]) for row in observed]
    by_asset = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    by_side = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    by_horizon = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    for row in trades:
        asset = str(row.get("asset") or "UNKNOWN")
        side = str(row.get("side") or "SELECTED")
        horizon = str(row.get("exit_horizon_ms"))
        by_asset[asset]["trades"] += 1
        by_side[side]["trades"] += 1
        by_horizon[horizon]["trades"] += 1
        if row.get("realized_pnl") is not None:
            value = float(row["realized_pnl"])
            for cell in (by_asset[asset], by_side[side], by_horizon[horizon]):
                cell["observed"] += 1
                cell["pnl"] += value
    return {
        "schema": SCHEMA + "_summary_v2",
        **SAFETY,
        "opportunities": len(outcomes),
        "selected_trades": len(trades),
        "no_trade": len(outcomes) - len(trades),
        "observed_selected_trades": len(observed),
        "censored_selected_trades": len(trades) - len(observed),
        "positive_observed_trades": sum(value > 0 for value in pnl),
        "zero_observed_trades": sum(abs(value) <= 1e-15 for value in pnl),
        "negative_observed_trades": sum(value < 0 for value in pnl),
        "total_observed_net_pnl": sum(pnl) if pnl else None,
        "mean_observed_net_pnl": sum(pnl) / len(pnl) if pnl else None,
        "mean_predicted_policy_utility": (
            sum(float(row.get("policy_utility") or 0.0) for row in trades) / len(trades)
            if trades else None
        ),
        "total_predicted_residual_friction": sum(
            float(row.get("total_residual_friction") or 0.0) for row in trades),
        "total_predicted_uncertainty_penalty": sum(
            float(row.get("uncertainty_penalty") or 0.0) for row in trades),
        "total_predicted_selection_optimism_penalty": sum(
            float(row.get("selection_optimism_penalty") or 0.0)
            for row in trades),
        "selected_size_distribution": numeric_distribution(
            row.get("size") for row in trades),
        "selected_notional_distribution": numeric_distribution(
            row.get("notional") for row in trades),
        "observed_pnl_distribution": numeric_distribution(pnl),
        "max_active_positions": max(
            (int(row.get("replay_max_active_positions") or 0) for row in outcomes),
            default=0),
        "max_gross_notional": max(
            (float(row.get("replay_max_gross_notional") or 0.0) for row in outcomes),
            default=0.0),
        "by_asset": dict(by_asset),
        "by_side": dict(by_side),
        "by_exit_horizon_ms": dict(by_horizon),
    }


def merge_direct_action_summaries(summaries):
    summaries = list(summaries)
    if not summaries:
        return {
            "schema": SCHEMA + "_summary_v2", **SAFETY,
            "opportunities": 0, "selected_trades": 0, "no_trade": 0,
            "observed_selected_trades": 0, "censored_selected_trades": 0,
        }

    additive = (
        "opportunities", "selected_trades", "no_trade",
        "observed_selected_trades", "censored_selected_trades",
        "positive_observed_trades", "zero_observed_trades",
        "negative_observed_trades", "total_predicted_residual_friction",
        "total_predicted_uncertainty_penalty",
        "total_predicted_selection_optimism_penalty",
    )
    result = {"schema": SCHEMA + "_summary_v2", **SAFETY}
    for key in additive:
        result[key] = sum(float(summary.get(key) or 0) for summary in summaries)
        if key not in (
            "total_predicted_residual_friction",
            "total_predicted_uncertainty_penalty",
            "total_predicted_selection_optimism_penalty",
        ):
            result[key] = int(result[key])

    observed = result["observed_selected_trades"]
    pnl_total = sum(
        float(summary.get("total_observed_net_pnl") or 0.0)
        for summary in summaries
    )
    result["total_observed_net_pnl"] = pnl_total if observed else None
    result["mean_observed_net_pnl"] = pnl_total / observed if observed else None

    selected = result["selected_trades"]
    utility_total = sum(
        float(summary.get("mean_predicted_policy_utility") or 0.0)
        * int(summary.get("selected_trades") or 0)
        for summary in summaries
    )
    result["mean_predicted_policy_utility"] = (
        utility_total / selected if selected else None
    )
    result["max_active_positions"] = max(
        int(summary.get("max_active_positions") or 0) for summary in summaries)
    result["max_gross_notional"] = max(
        float(summary.get("max_gross_notional") or 0.0) for summary in summaries)

    for dimension in ("by_asset", "by_side", "by_exit_horizon_ms"):
        cells = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
        for summary in summaries:
            for name, source in (summary.get(dimension) or {}).items():
                cell = cells[str(name)]
                cell["trades"] += int(source.get("trades") or 0)
                cell["observed"] += int(source.get("observed") or 0)
                cell["pnl"] += float(source.get("pnl") or 0.0)
        result[dimension] = dict(cells)

    result["selected_size_distribution"] = {
        "state": "SEE_EXACT_PER_FOLD_DISTRIBUTIONS",
        "folds": [summary["selected_size_distribution"] for summary in summaries],
    }
    result["selected_notional_distribution"] = {
        "state": "SEE_EXACT_PER_FOLD_DISTRIBUTIONS",
        "folds": [summary["selected_notional_distribution"] for summary in summaries],
    }
    result["observed_pnl_distribution"] = {
        "state": "SEE_EXACT_PER_FOLD_DISTRIBUTIONS",
        "folds": [summary["observed_pnl_distribution"] for summary in summaries],
    }
    return result


def summarize_effective_age_buckets(outcomes):
    cells = defaultdict(lambda: {
        "selected_trades": 0,
        "observed_selected_trades": 0,
        "censored_selected_trades": 0,
        "positive_observed_trades": 0,
        "zero_observed_trades": 0,
        "negative_observed_trades": 0,
        "total_observed_net_pnl": 0.0,
        "predicted_policy_utility_sum": 0.0,
    })
    for row in outcomes:
        if row.get("action") != "TRADE":
            continue
        bucket = effective_age_bucket(
            float(row.get("signal_age_ms") or 0.0),
            int(row.get("latency_ms") or 0),
        )
        cell = cells[bucket]
        cell["selected_trades"] += 1
        cell["predicted_policy_utility_sum"] += float(
            row.get("policy_utility") or 0.0)
        realized = row.get("realized_pnl")
        if realized is None:
            cell["censored_selected_trades"] += 1
            continue
        realized = float(realized)
        cell["observed_selected_trades"] += 1
        cell["total_observed_net_pnl"] += realized
        if realized > 0:
            cell["positive_observed_trades"] += 1
        elif realized < 0:
            cell["negative_observed_trades"] += 1
        else:
            cell["zero_observed_trades"] += 1

    output = {}
    for bucket, cell in sorted(cells.items()):
        selected = cell["selected_trades"]
        observed = cell["observed_selected_trades"]
        output[bucket] = {
            **cell,
            "observed_fraction": observed / selected if selected else None,
            "mean_observed_net_pnl": (
                cell["total_observed_net_pnl"] / observed
                if observed else None),
            "mean_predicted_policy_utility": (
                cell["predicted_policy_utility_sum"] / selected
                if selected else None),
        }
        output[bucket].pop("predicted_policy_utility_sum", None)
    return output


def evaluate_latency_age_surface(
    model,
    rows,
    *,
    latencies_ms=(25, 50, 100, 250),
    capital_budget=10_000.0,
):
    """Diagnostic OOS latency/age decay surface; never selects a policy."""
    entries = []
    original_gate = model.maximum_effective_action_age_ms
    try:
        model.maximum_effective_action_age_ms = None
        for latency_ms in latencies_ms:
            latency_ms = int(latency_ms)
            if latency_ms not in model.train_latencies_ms:
                entries.append({
                    "latency_ms": latency_ms,
                    "state": "OUTSIDE_MODEL_LATENCY_SUPPORT",
                })
                continue
            outcomes = evaluate_direct_action_policy(
                model,
                rows,
                latency_ms=latency_ms,
                capital_budget=capital_budget,
                one_entry_per_market=True,
                live_geometry=True,
            )
            selected = [
                row for row in outcomes if row.get("action") == "TRADE"]
            entries.append({
                "latency_ms": latency_ms,
                "state": "READY",
                "selection": "NONE_DIAGNOSTIC_ONLY",
                "summary": summarize_direct_action(outcomes),
                "selected_effective_age_ms": numeric_distribution(
                    row.get("effective_action_age_ms") for row in selected),
                "by_effective_age_bucket": summarize_effective_age_buckets(
                    outcomes),
            })
    finally:
        model.maximum_effective_action_age_ms = original_gate
    return {
        "schema": SCHEMA + "_latency_age_surface_v1",
        **SAFETY,
        "selection": "NONE_DIAGNOSTIC_ONLY",
        "selection_warning": (
            "LATENCY_AND_AGE_BUCKETS_ARE_DIAGNOSTIC_OOS_CELLS;"
            "DO_NOT_PICK_A_GATE_FROM_THE_SAME_OUTER_OOS"
        ),
        "effective_age_semantics": (
            "DECISION_SIGNAL_AGE_MS_PLUS_MODELED_EXECUTION_LATENCY_MS"),
        "entries": entries,
    }


def walk_forward_direct_action(
    records,
    *,
    desired_folds=3,
    latency_ms=50,
    capital_budget=10_000.0,
    model_kwargs=None,
):
    found, receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA + "_walk_forward_v1",
        **SAFETY,
        "state": receipt.get("state"),
        "fold_receipt": receipt,
        "latency_ms": int(latency_ms),
        "capital_budget": float(capital_budget),
        "bilateral_evidence": bilateral_evidence_summary(records),
        "folds": [],
        "diagnostic_selected_outcomes": [],
        "output_semantics": (
            "FULL_OOS_OUTCOMES_ARE_SUMMARIZED_NOT_SERIALIZED;"
            "BOUNDED_SELECTED_TRADE_DIAGNOSTICS_ONLY"
        ),
        "mean_covariance_estimation": False,
    }
    if not found:
        return result
    fold_summaries = []
    for fold in found:
        model = DirectActionValueModel(**(model_kwargs or {})).fit(fold["train_repricing"])
        outcomes = evaluate_direct_action_policy(
            model, fold["test"], latency_ms=latency_ms,
            capital_budget=capital_budget, one_entry_per_market=True,
            live_geometry=True)
        summary = summarize_direct_action(outcomes)
        fold_summaries.append(summary)
        result["folds"].append({
            "fold": fold["fold"],
            "cutoff_ns": fold["cutoff_ns"],
            "train_markets": len(fold["train_markets"]),
            "test_markets": len(fold["test_markets"]),
            "training": model.training_receipt,
            "oos": summary,
            "latency_age_surface": evaluate_latency_age_surface(
                model,
                fold["test"],
                latencies_ms=DEFAULT_TRAIN_LATENCIES_MS,
                capital_budget=capital_budget,
            ),
        })
        remaining = max(0, 384 - len(result["diagnostic_selected_outcomes"]))
        if remaining:
            for row in (value for value in outcomes if value.get("action") == "TRADE"):
                result["diagnostic_selected_outcomes"].append({
                    key: row.get(key) for key in (
                        "market_id", "asset", "contract_horizon", "decision_ns",
                        "side", "size", "exit_horizon_ms", "latency_ms",
                        "signal_age_ms", "effective_action_age_ms", "notional",
                        "policy_utility", "predicted_total_net_cash_pnl",
                        "uncertainty_penalty", "selection_optimism_penalty",
                        "total_residual_friction",
                        "realized_pnl", "target_state",
                        "censored_worst_case_pnl",
                        "censored_worst_case_loss_bound",
                    )
                })
                remaining -= 1
                if remaining <= 0:
                    break
    result["summary"] = merge_direct_action_summaries(fold_summaries)
    result["state"] = "READY"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Direct action value PAPER research")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--latency-ms", type=int, default=50)
    parser.add_argument("--capital-budget", type=float, default=10_000.0)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--minimum-wall-ns", type=int, default=None)
    args = parser.parse_args(argv)

    data = build_dataset(
        args.root,
        **({"minimum_wall_ns": args.minimum_wall_ns}
           if args.minimum_wall_ns is not None else {}))
    if data.get("input_state") != "READY":
        result = {
            "schema": SCHEMA + "_walk_forward_v1",
            **SAFETY,
            "state": data.get("input_state"),
            "data_sha256": data.get("data_sha256"),
        }
    else:
        result = walk_forward_direct_action(
            data["decisions"], desired_folds=args.folds,
            latency_ms=args.latency_ms, capital_budget=args.capital_budget)
        result["data_sha256"] = data.get("data_sha256")
    atomic_json(args.output, result)
    return 0 if result.get("state") == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
