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
DEFAULT_TRAIN_LATENCIES_MS = (50,)
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


def candidate_sizes(row, *, size_grid=DEFAULT_SIZE_GRID,
                    hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                    available_capital=None, max_sizes=5):
    """Decision-time feasible size support.  No future depth enters here."""
    if not _valid_state(row):
        return []
    ask = float(row["ask"])
    minimum = float(row["minimum"])
    depth = float(row["quantity"])
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


def realized_action_economics(row, *, size, horizon_ms, latency_ms,
                              entry_cap=DEFAULT_ENTRY_CAP,
                              hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                              require_full_decision_depth=True):
    """Observed execution economics for one counterfactual action.

    The returned cash PnL already includes the observable trading frictions:
    entry spread, realized post-signal/latency price drift, exit spread,
    partial fill/no-fill and both taker fees.  Missing evidence is censored.
    """
    if horizon_ms <= latency_ms:
        return None, "HORIZON_NOT_AFTER_EXECUTION"
    if not _valid_state(row):
        return None, "STATE_OUTSIDE_RESEARCH_SUPPORT"
    ask0 = float(row["ask"])
    bid0 = float(row["bid"])
    size = float(size)
    if ask0 > entry_cap + 1e-12:
        return None, "ENTRY_CAP"
    if size + 1e-12 < float(row["minimum"]) or size <= 0:
        return None, "BELOW_VENUE_MINIMUM"
    if require_full_decision_depth and size > float(row["quantity"]) + 1e-12:
        return None, "INSUFFICIENT_DECISION_DEPTH"
    if size * ask0 > float(hard_order_notional) + 1e-9:
        return None, "ORDER_NOTIONAL_CAP"

    book, why = arrival(row, int(latency_ms))
    if book is None:
        return None, str(why or "ARRIVAL_UNAVAILABLE")

    # Zero chase.  A causally observed non-fill is a real zero payoff for this
    # action, not censored evidence.
    if float(book["ask"]) > ask0 + 1e-12:
        return {
            "cash_pnl": 0.0,
            "gross_executable_markout": 0.0,
            "filled": 0.0,
            "requested": size,
            "entry_price": None,
            "exit_bid": None,
            "entry_fee": 0.0,
            "exit_fee": 0.0,
            "total_fees": 0.0,
            "decision_half_spread_cost": 0.0,
            "latency_price_drift_cost": 0.0,
            "exit_half_spread_cost": 0.0,
            "ideal_midpoint_alpha": None,
            "frictions_embedded_in_cash_pnl": True,
        }, "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    fill = min(size, float(book.get("quantity") or 0.0))
    if fill <= 0:
        return {
            "cash_pnl": 0.0,
            "gross_executable_markout": 0.0,
            "filled": 0.0,
            "requested": size,
            "entry_price": None,
            "exit_bid": None,
            "entry_fee": 0.0,
            "exit_fee": 0.0,
            "total_fees": 0.0,
            "decision_half_spread_cost": 0.0,
            "latency_price_drift_cost": 0.0,
            "exit_half_spread_cost": 0.0,
            "ideal_midpoint_alpha": None,
            "frictions_embedded_in_cash_pnl": True,
        }, "OBSERVED_NO_FILL_ZERO_DEPTH"

    target = row.get("targets", {}).get(str(int(horizon_ms)), {})
    if target.get("state") != "OBSERVED" or not finite(target.get("arrival_bid")):
        return None, "EXIT_EVIDENCE_UNAVAILABLE"

    entry_price = float(book["ask"])
    exit_bid = float(target["arrival_bid"])
    entry_fee = fee_per_share(row, entry_price) * fill
    exit_fee = fee_per_share(row, exit_bid) * fill
    gross_executable = fill * (exit_bid - entry_price)
    cash_pnl = gross_executable - entry_fee - exit_fee

    decision_mid = (bid0 + ask0) / 2.0
    future_ask = target.get("arrival_ask")
    future_mid = (
        (exit_bid + float(future_ask)) / 2.0
        if finite(future_ask) and float(future_ask) > exit_bid
        else None
    )
    ideal_midpoint_alpha = (
        fill * (future_mid - decision_mid) if future_mid is not None else None
    )
    decision_half_spread = fill * (ask0 - decision_mid)
    latency_price_drift = fill * (entry_price - ask0)
    exit_half_spread = (
        fill * (future_mid - exit_bid) if future_mid is not None else None
    )
    state = "OBSERVED_FULL_FILL" if fill + 1e-12 >= size else "OBSERVED_PARTIAL_FILL"
    return {
        "cash_pnl": float(cash_pnl),
        "gross_executable_markout": float(gross_executable),
        "filled": float(fill),
        "requested": float(size),
        "entry_price": entry_price,
        "exit_bid": exit_bid,
        "entry_fee": float(entry_fee),
        "exit_fee": float(exit_fee),
        "total_fees": float(entry_fee + exit_fee),
        "decision_half_spread_cost": float(decision_half_spread),
        # Signed: negative means latency gave price improvement.  No-fill due to
        # adverse drift is represented by the explicit zero-payoff state above.
        "latency_price_drift_cost": float(latency_price_drift),
        "exit_half_spread_cost": (
            float(exit_half_spread) if exit_half_spread is not None else None
        ),
        "ideal_midpoint_alpha": (
            float(ideal_midpoint_alpha) if ideal_midpoint_alpha is not None else None
        ),
        "frictions_embedded_in_cash_pnl": True,
    }, state


def realized_action_value(row, *, size, horizon_ms, latency_ms,
                          entry_cap=DEFAULT_ENTRY_CAP,
                          hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                          require_full_decision_depth=True):
    economics, state = realized_action_economics(
        row, size=size, horizon_ms=horizon_ms, latency_ms=latency_ms,
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
    direction = 1.0 if float(row.get("direction") or 0) >= 0 else -1.0
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
        if self.max_sizes_per_state <= 0 or self.streaming_batch_size <= 0:
            raise ValueError("positive direct-action capacity limits required")
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
        names = [
            "state.ask", "state.bid", "state.spread", "state.depth",
            "state.minimum", "state.tte_s", "state.signal_age_ms",
            "state.direction", "action.size", "action.size2",
            "action.log_size", "action.depth_fraction", "action.notional",
            "action.notional_fraction_of_cap", "action.exit_horizon_ms",
            "action.log_exit_horizon", "system.latency_ms",
            "system.log_latency", "interaction.size_signal",
            "interaction.size_abs_signal", "interaction.size_spread",
            "interaction.size2_over_depth", "interaction.horizon_signal",
            "interaction.horizon_abs_signal",
        ]
        names.extend("x." + name for name in self.base_names)
        names.extend("asset::" + asset for asset in self.assets)
        names.extend("contract::" + horizon for horizon in self.contract_horizons)
        names.extend("exit::" + str(h) for h in self.action_horizons_ms)
        names.extend("latency::" + str(v) for v in self.train_latencies_ms)
        self.model_feature_names = tuple(names)

    def _action_record(self, row, *, size, horizon_ms, latency_ms):
        ask = float(row["ask"])
        bid = float(row["bid"])
        depth = max(1e-12, float(row["quantity"]))
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
            "state.signal_age_ms": float(row.get("signal_age_ns") or 0) / 1e6,
            "state.direction": float(row.get("direction") or 0),
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
            "interaction.size_signal": float(size) * signal,
            "interaction.size_abs_signal": float(size) * abs(signal),
            "interaction.size_spread": float(size) * (ask - bid),
            "interaction.size2_over_depth": float(size) ** 2 / depth,
            "interaction.horizon_signal": math.log1p(float(horizon_ms)) * signal,
            "interaction.horizon_abs_signal": math.log1p(float(horizon_ms)) * abs(signal),
        }
        source = row.get("features", {})
        for name in self.base_names:
            value = source.get(name)
            if finite(value):
                features["x." + name] = float(value)
        asset = str(row.get("asset") or "UNKNOWN")
        contract = str(row.get("horizon") or "UNKNOWN")
        for value in self.assets:
            features["asset::" + value] = 1.0 if value == asset else 0.0
        for value in self.contract_horizons:
            features["contract::" + value] = 1.0 if value == contract else 0.0
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
            "size": float(size),
            "exit_horizon_ms": int(horizon_ms),
            "latency_ms": int(latency_ms),
        }

    def _iter_training_actions(self, rows, *, markets=None, state_counter=None):
        market_filter = set(markets) if markets is not None else None
        for row in rows:
            if market_filter is not None and str(row["market_id"]) not in market_filter:
                continue
            if not _valid_state(row):
                if state_counter is not None:
                    state_counter["STATE_OUTSIDE_RESEARCH_SUPPORT"] += 1
                continue
            sizes = candidate_sizes(
                row, size_grid=self.size_grid,
                hard_order_notional=self.hard_order_notional,
                max_sizes=self.max_sizes_per_state,
            )
            if not sizes:
                if state_counter is not None:
                    state_counter["NO_FEASIBLE_SIZE"] += 1
                continue
            for latency in self.train_latencies_ms:
                for horizon in self.action_horizons_ms:
                    if horizon <= latency:
                        continue
                    for size in sizes:
                        target, state = realized_action_value(
                            row, size=size, horizon_ms=horizon, latency_ms=latency,
                            entry_cap=self.entry_cap,
                            hard_order_notional=self.hard_order_notional,
                        )
                        if state_counter is not None:
                            state_counter[state] += 1
                        if target is None:
                            continue
                        action = self._action_record(
                            row, size=size, horizon_ms=horizon, latency_ms=latency)
                        action["target"] = float(target)
                        action["target_state"] = state
                        yield action


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


    def fit(self, rows):
        rows = list(rows)
        if not rows:
            raise ValueError("direct action training rows required")
        self._configure_levels(rows)
        markets = self._market_order(rows)
        if not markets:
            raise ValueError("no admissible direct-action training markets")

        def factory(market_subset=None, counter=None):
            return lambda: self._iter_training_actions(
                rows, markets=market_subset, state_counter=counter)

        self.uncertainty_floor = 1e-6
        self.calibration_multiplier = 1.5
        self.scale_model = None
        calibration_state = "INSUFFICIENT_MARKET_BLOCKS"

        # Temporal market blocks are defined before any outcome-dependent fit.
        fit_markets = scale_markets = calibration_markets = set()
        if len(markets) >= 15:
            fit_end = max(1, int(len(markets) * 0.60))
            scale_end = max(fit_end + 1, int(len(markets) * 0.80))
            scale_end = min(scale_end, len(markets) - 1)
            fit_markets = set(markets[:fit_end])
            scale_markets = set(markets[fit_end:scale_end])
            calibration_markets = set(markets[scale_end:])

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
            # Residual target mean is a stable, bounded-memory scale floor.
            self.uncertainty_floor = max(
                1e-6, 0.10 * self.scale_model.target_mean)

            # Conformal calibration is market-blocked: one worst normalized
            # residual per market, so millions of within-market action variants
            # do not masquerade as independent calibration observations.
            block_scores = {}
            for action in factory(calibration_markets)():
                predicted = provisional.predict(action)
                scale = max(
                    self.uncertainty_floor,
                    self.scale_model.predict(action))
                score = abs(float(action["target"]) - predicted) / scale
                market = str(action["market_id"])
                block_scores[market] = max(
                    float(score), block_scores.get(market, 0.0))
            calibrated = _quantile(
                list(block_scores.values()), self.calibration_level)
            if calibrated is not None and finite(calibrated):
                self.calibration_multiplier = max(1.0, float(calibrated))
                calibration_state = "TEMPORAL_MARKET_BLOCK_CONFORMAL"

        target_states = Counter()
        final_mean = StreamingRidge(
            self.model_feature_names, ridge=self.ridge,
            batch_size=self.streaming_batch_size).fit_factory(
                factory(None, target_states),
                lambda action: action["target"])

        if self.scale_model is None:
            # Small-sample fallback is intentionally conservative and not a
            # coverage claim.  Real London runs have many market blocks.
            self.uncertainty_floor = max(1e-6, final_mean.target_std)

        self.mean_model = final_mean
        self.training_receipt = {
            "schema": SCHEMA + "_training_v1",
            **SAFETY,
            "state": "READY",
            "training_states_total": len(rows),
            "training_states_used": len(rows),
            "training_markets_total": len({str(row["market_id"]) for row in rows}),
            "training_markets_used": len(markets),
            "training_state_cap": None,
            "action_targets": final_mean.rows,
            "target_state_counts": dict(target_states),
            "size_grid": list(self.size_grid),
            "action_horizons_ms": list(self.action_horizons_ms),
            "train_latencies_ms": list(self.train_latencies_ms),
            "latency_role": "CONDITIONING_STATE_NOT_OPTIMIZED_ACTION",
            "action_space": ["NO_TRADE", "SIGNALED_SIDE_X_SIZE_X_EXIT_HORIZON"],
            "opposite_side_counterfactual": "UNAVAILABLE_UNTIL_BOTH_SIDES_ARE_CAPTURED_CAUSALLY",
            "entry_cap": self.entry_cap,
            "hard_order_notional": self.hard_order_notional,
            "model": "STREAMING_RIDGE_DIRECT_EXECUTABLE_CASH_PNL",
            "matrix_strategy": "ONE_PASS_SUFFICIENT_STATISTICS_THEN_P_X_P_NORMAL_EQUATIONS",
            "design_dimension": final_mean.design_dimension,
            "gram_matrix_bytes": final_mean.gram_bytes,
            "maximum_streaming_batch_bytes": final_mean.maximum_batch_bytes,
            "regularized_gram_condition_number": final_mean.condition_number,
            "streaming_batch_size": self.streaming_batch_size,
            "policy_objective": "PREDICTED_EXECUTABLE_CASH_PNL_MINUS_UNCERTAINTY_MINUS_RESIDUAL_PORTFOLIO_FRICTIONS",
            "policy_loss": "NEGATIVE_POLICY_UTILITY_WITH_NO_TRADE_BASELINE_ZERO",
            "execution_frictions_in_training_target": [
                "decision_spread_via_executable_entry",
                "post_signal_latency_price_drift_via_arrival_book",
                "fill_and_no_fill",
                "partial_fill",
                "exit_spread_via_executable_bid",
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
            "uncertainty": "STREAMING_ABSOLUTE_RESIDUAL_SCALE_WITH_TEMPORAL_MARKET_BLOCK_CONFORMAL",
            "calibration_level": self.calibration_level,
            "calibration_multiplier": self.calibration_multiplier,
            "uncertainty_floor": self.uncertainty_floor,
            "calibration_state": calibration_state,
            "feature_names": list(self.model_feature_names),
            "capacity_scope": "L1_ONLY_NO_COUNTERFACTUAL_IMPACT_BEYOND_VISIBLE_DEPTH",
            "mean_covariance_estimation": False,
        }
        self.fitted = True
        return self

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
        if live_geometry and not (
            LIVE_MINIMUM_TTE_NS <= int(row["tte_ns"]) <= LIVE_MAXIMUM_TTE_NS
            and float(row["ask"]) <= self.entry_cap + 1e-12
        ):
            return [], "OUTSIDE_LIVE_GEOMETRY"
        sizes = candidate_sizes(
            row, size_grid=self.size_grid,
            hard_order_notional=self.hard_order_notional,
            available_capital=available_capital,
            max_sizes=self.max_sizes_per_state,
        )
        if not sizes:
            return [], "NO_FEASIBLE_SIZE"
        action_rows = []
        for horizon in self.action_horizons_ms:
            if horizon <= latency_ms:
                continue
            for size in sizes:
                action_rows.append(self._action_record(
                    row, size=size, horizon_ms=horizon, latency_ms=latency_ms))
        if not action_rows:
            return [], "NO_FEASIBLE_ACTION"
        means = self.mean_model.predict_many(action_rows)
        if self.scale_model is None:
            scales = [self.uncertainty_floor] * len(action_rows)
        else:
            scales = [
                max(self.uncertainty_floor, value)
                for value in self.scale_model.predict_many(action_rows)
            ]
        scored = []
        for action, mean, scale in zip(action_rows, means, scales):
            notional = float(action["size"]) * float(row["ask"])
            uncertainty_penalty = (
                float(self.friction_policy.uncertainty_aversion)
                * self.calibration_multiplier * float(scale)
            )
            base = {
                "action": "TRADE",
                "size": float(action["size"]),
                "exit_horizon_ms": int(action["exit_horizon_ms"]),
                "latency_ms": latency_ms,
                "notional": notional,
            }
            residual = residual_policy_friction(
                base, row, portfolio_state=portfolio_state,
                capital_budget=capital_budget,
                friction_policy=self.friction_policy)
            lower_cash = float(mean) - uncertainty_penalty
            policy_utility = lower_cash - residual["total_residual_friction"]
            scored.append({
                **base,
                "predicted_total_net_cash_pnl": float(mean),
                "predicted_total_net_pnl": float(mean),
                "predicted_abs_error_scale": float(scale),
                "uncertainty_penalty": float(uncertainty_penalty),
                "calibrated_lower_cash_value": float(lower_cash),
                "calibrated_lower_value": float(policy_utility),
                "policy_utility": float(policy_utility),
                "policy_loss": float(-policy_utility),
                **residual,
                "predicted_return_on_notional": (
                    float(mean) / notional if notional > 0 else None),
            })
        scored.sort(
            key=lambda value: (
                value["policy_utility"],
                value["predicted_total_net_cash_pnl"],
                -value["notional"],
            ),
            reverse=True,
        )
        return scored, "READY"

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
        direction = 1.0 if float(row.get("direction") or 0) >= 0 else -1.0
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


def summarize_direct_action(outcomes):
    trades = [row for row in outcomes if row.get("action") == "TRADE"]
    observed = [row for row in trades if row.get("realized_pnl") is not None]
    pnl = [float(row["realized_pnl"]) for row in observed]
    by_asset = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    by_size = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    by_horizon = defaultdict(lambda: {"trades": 0, "observed": 0, "pnl": 0.0})
    for row in trades:
        asset = str(row.get("asset") or "UNKNOWN")
        size = str(row.get("size"))
        horizon = str(row.get("exit_horizon_ms"))
        by_asset[asset]["trades"] += 1
        by_size[size]["trades"] += 1
        by_horizon[horizon]["trades"] += 1
        if row.get("realized_pnl") is not None:
            value = float(row["realized_pnl"])
            for cell in (by_asset[asset], by_size[size], by_horizon[horizon]):
                cell["observed"] += 1
                cell["pnl"] += value
    return {
        "schema": SCHEMA + "_summary_v1",
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
        "max_active_positions": max(
            (int(row.get("replay_max_active_positions") or 0) for row in outcomes),
            default=0),
        "max_gross_notional": max(
            (float(row.get("replay_max_gross_notional") or 0.0) for row in outcomes),
            default=0.0),
        "by_asset": dict(by_asset),
        "by_size": dict(by_size),
        "by_exit_horizon_ms": dict(by_horizon),
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
        "folds": [],
        "outcomes": [],
        "mean_covariance_estimation": False,
    }
    if not found:
        return result
    for fold in found:
        model = DirectActionValueModel(**(model_kwargs or {})).fit(fold["train_repricing"])
        outcomes = evaluate_direct_action_policy(
            model, fold["test"], latency_ms=latency_ms,
            capital_budget=capital_budget, one_entry_per_market=True,
            live_geometry=True)
        result["folds"].append({
            "fold": fold["fold"],
            "cutoff_ns": fold["cutoff_ns"],
            "train_markets": len(fold["train_markets"]),
            "test_markets": len(fold["test_markets"]),
            "training": model.training_receipt,
            "oos": summarize_direct_action(outcomes),
        })
        result["outcomes"].extend(outcomes)
    result["summary"] = summarize_direct_action(result["outcomes"])
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
