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
import argparse
import math
from pathlib import Path
from statistics import median

from research.walk_forward_v2.core import (
    SAFETY,
    Ridge,
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


def realized_action_value(row, *, size, horizon_ms, latency_ms,
                          entry_cap=DEFAULT_ENTRY_CAP,
                          hard_order_notional=DEFAULT_HARD_ORDER_NOTIONAL,
                          require_full_decision_depth=True):
    """Observed total net executable markout for one counterfactual action.

    A causally observed no-fill is worth exactly zero.  Missing arrival or exit
    evidence is censored and returns None.
    """
    if horizon_ms <= latency_ms:
        return None, "HORIZON_NOT_AFTER_EXECUTION"
    if not _valid_state(row):
        return None, "STATE_OUTSIDE_RESEARCH_SUPPORT"
    ask0 = float(row["ask"])
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

    # Zero chase: the order is never allowed to pay above the causal decision ask.
    if float(book["ask"]) > ask0 + 1e-12:
        return 0.0, "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    fill = min(size, float(book.get("quantity") or 0.0))
    if fill <= 0:
        return 0.0, "OBSERVED_NO_FILL_ZERO_DEPTH"

    target = row.get("targets", {}).get(str(int(horizon_ms)), {})
    if target.get("state") != "OBSERVED" or not finite(target.get("arrival_bid")):
        return None, "EXIT_EVIDENCE_UNAVAILABLE"
    exit_bid = float(target["arrival_bid"])
    entry_price = float(book["ask"])
    entry_fee = fee_per_share(row, entry_price) * fill
    exit_fee = fee_per_share(row, exit_bid) * fill
    pnl = fill * (exit_bid - entry_price) - entry_fee - exit_fee
    state = "OBSERVED_FULL_FILL" if fill + 1e-12 >= size else "OBSERVED_PARTIAL_FILL"
    return float(pnl), state


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
        max_sizes_per_state=5,
    ):
        self.size_grid = tuple(float(v) for v in size_grid)
        self.action_horizons_ms = tuple(int(v) for v in action_horizons_ms)
        self.train_latencies_ms = tuple(int(v) for v in train_latencies_ms)
        self.entry_cap = float(entry_cap)
        self.hard_order_notional = float(hard_order_notional)
        self.ridge = float(ridge)
        self.calibration_level = float(calibration_level)
        self.max_sizes_per_state = int(max_sizes_per_state)
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

    def _expand_training_actions(self, rows):
        actions = []
        states = Counter()
        for row in rows:
            if not _valid_state(row):
                states["STATE_OUTSIDE_RESEARCH_SUPPORT"] += 1
                continue
            sizes = candidate_sizes(
                row, size_grid=self.size_grid,
                hard_order_notional=self.hard_order_notional,
                max_sizes=self.max_sizes_per_state,
            )
            if not sizes:
                states["NO_FEASIBLE_SIZE"] += 1
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
                        states[state] += 1
                        if target is None:
                            continue
                        action = self._action_record(
                            row, size=size, horizon_ms=horizon, latency_ms=latency)
                        action["target"] = float(target)
                        action["target_state"] = state
                        actions.append(action)
        return actions, states

    @staticmethod
    def _market_order(actions):
        first = {}
        for row in actions:
            market = row["market_id"]
            first[market] = min(first.get(market, row["decision_ns"]), row["decision_ns"])
        return sorted(first, key=lambda market: (first[market], market))

    def fit(self, rows):
        rows = list(rows)
        if not rows:
            raise ValueError("direct action training rows required")
        self._configure_levels(rows)
        actions, target_states = self._expand_training_actions(rows)
        if len(actions) < 64:
            raise ValueError("insufficient observed direct-action targets")

        markets = self._market_order(actions)
        final_mean = Ridge(self.model_feature_names, ridge=self.ridge).fit(
            actions, lambda row: row["target"])

        self.uncertainty_floor = 1e-6
        self.calibration_multiplier = 1.0
        self.scale_model = None
        calibration_state = "IN_SAMPLE_FALLBACK"

        if len(markets) >= 15:
            fit_end = max(1, int(len(markets) * 0.60))
            scale_end = max(fit_end + 1, int(len(markets) * 0.80))
            scale_end = min(scale_end, len(markets) - 1)
            fit_markets = set(markets[:fit_end])
            scale_markets = set(markets[fit_end:scale_end])
            calibration_markets = set(markets[scale_end:])
            fit_rows = [row for row in actions if row["market_id"] in fit_markets]
            scale_rows = [row for row in actions if row["market_id"] in scale_markets]
            calibration_rows = [
                row for row in actions if row["market_id"] in calibration_markets]
            if fit_rows and scale_rows and calibration_rows:
                provisional = Ridge(self.model_feature_names, ridge=self.ridge).fit(
                    fit_rows, lambda row: row["target"])
                scale_training = []
                scale_errors = []
                for row, predicted in zip(scale_rows, provisional.predict_many(scale_rows)):
                    clone = dict(row)
                    clone["features"] = dict(row["features"])
                    clone["abs_error"] = abs(float(row["target"]) - float(predicted))
                    scale_training.append(clone)
                    scale_errors.append(clone["abs_error"])
                self.uncertainty_floor = max(
                    1e-6, float(median(scale_errors)) * 0.10 if scale_errors else 1e-6)
                if len(scale_training) >= 32:
                    self.scale_model = Ridge(
                        self.model_feature_names, ridge=self.ridge).fit(
                            scale_training, lambda row: row["abs_error"])
                    ratios = []
                    calibration_predictions = provisional.predict_many(calibration_rows)
                    scale_predictions = self.scale_model.predict_many(calibration_rows)
                    for row, predicted, scale in zip(
                            calibration_rows, calibration_predictions, scale_predictions):
                        denom = max(self.uncertainty_floor, float(scale))
                        ratios.append(abs(float(row["target"]) - float(predicted)) / denom)
                    calibrated = _quantile(ratios, self.calibration_level)
                    if calibrated is not None and finite(calibrated):
                        self.calibration_multiplier = max(1.0, float(calibrated))
                    calibration_state = "MARKET_BLOCK_TEMPORAL_CALIBRATION"

        if self.scale_model is None:
            predictions = final_mean.predict_many(actions)
            errors = [abs(float(row["target"]) - pred)
                      for row, pred in zip(actions, predictions)]
            self.uncertainty_floor = max(
                1e-6, float(median(errors)) if errors else 1e-6)
            # No formal coverage claim under fallback.
            self.calibration_multiplier = 1.5

        self.mean_model = final_mean
        self.training_receipt = {
            "schema": SCHEMA + "_training_v1",
            **SAFETY,
            "state": "READY",
            "training_states": len(rows),
            "training_markets": len({str(row["market_id"]) for row in rows}),
            "action_targets": len(actions),
            "target_state_counts": dict(target_states),
            "size_grid": list(self.size_grid),
            "action_horizons_ms": list(self.action_horizons_ms),
            "train_latencies_ms": list(self.train_latencies_ms),
            "entry_cap": self.entry_cap,
            "hard_order_notional": self.hard_order_notional,
            "model": "RIDGE_DIRECT_TOTAL_NET_PNL",
            "ridge": self.ridge,
            "uncertainty": "ABSOLUTE_RESIDUAL_SCALE_WITH_TEMPORAL_MARKET_BLOCK_CALIBRATION",
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
                      live_geometry=True):
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
            lower = float(mean) - self.calibration_multiplier * float(scale)
            notional = float(action["size"]) * float(row["ask"])
            scored.append({
                "action": "TRADE",
                "size": float(action["size"]),
                "exit_horizon_ms": int(action["exit_horizon_ms"]),
                "latency_ms": latency_ms,
                "notional": notional,
                "predicted_total_net_pnl": float(mean),
                "predicted_abs_error_scale": float(scale),
                "calibrated_lower_value": lower,
                "predicted_return_on_notional": (
                    float(mean) / notional if notional > 0 else None),
            })
        scored.sort(
            key=lambda value: (
                value["calibrated_lower_value"],
                value["predicted_total_net_pnl"],
                -value["notional"],
            ),
            reverse=True,
        )
        return scored, "READY"

    def select_action(self, row, *, latency_ms=50, available_capital=None,
                      live_geometry=True, minimum_lower_value=0.0):
        scored, state = self.score_actions(
            row, latency_ms=latency_ms, available_capital=available_capital,
            live_geometry=live_geometry)
        if not scored or scored[0]["calibrated_lower_value"] <= float(minimum_lower_value):
            return {
                "action": "NO_TRADE",
                "reason": state if not scored else "LOWER_VALUE_NONPOSITIVE",
                "latency_ms": int(latency_ms),
                "calibrated_lower_value": 0.0,
                "predicted_total_net_pnl": 0.0,
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
    """Sequential OOS evaluation.  Selected but censored actions stay censored."""
    ordered = sorted(rows, key=lambda row: (row["decision_ns"], row["decision_id"]))
    used_markets = set()
    reserved = 0.0
    outcomes = []
    for row in ordered:
        market = str(row["market_id"])
        if one_entry_per_market and market in used_markets:
            outcomes.append({
                "market_id": market, "asset": row["asset"], "decision_ns": row["decision_ns"],
                "action": "NO_TRADE", "reason": "MARKET_ALREADY_TRADED",
                "realized_pnl": 0.0, "observed": True,
            })
            continue
        available = max(0.0, float(capital_budget) - reserved)
        selected = model.select_action(
            row, latency_ms=latency_ms, available_capital=available,
            live_geometry=live_geometry)
        outcome = {
            "market_id": market,
            "asset": row["asset"],
            "contract_horizon": row["horizon"],
            "decision_ns": row["decision_ns"],
            **selected,
        }
        if selected["action"] == "NO_TRADE":
            outcome.update({"realized_pnl": 0.0, "observed": True})
            outcomes.append(outcome)
            continue

        if one_entry_per_market:
            used_markets.add(market)
        reserved += float(selected["notional"])
        realized, target_state = realized_action_value(
            row,
            size=selected["size"],
            horizon_ms=selected["exit_horizon_ms"],
            latency_ms=latency_ms,
            entry_cap=model.entry_cap,
            hard_order_notional=model.hard_order_notional,
        )
        outcome["target_state"] = target_state
        outcome["observed"] = realized is not None
        outcome["realized_pnl"] = realized
        outcomes.append(outcome)
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
    args = parser.parse_args(argv)

    data = build_dataset(args.root)
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
