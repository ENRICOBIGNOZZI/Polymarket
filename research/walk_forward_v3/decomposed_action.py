"""Decomposed PAPER direct-action challenger.

Research only.  The baseline direct cash-PnL learner remains authoritative for
comparison.  This challenger separates execution probability from conditional
cash economics:

    E[cash PnL | state, action]
      = P(fill | state, action) * E[cash PnL | fill, state, action].

No real execution authority is introduced. Missing causal arrival/exit evidence
is excluded exactly as in the baseline action generator.
"""
from __future__ import annotations

from collections import defaultdict
import math

from research.walk_forward_v3 import direct_action as da


FILL_STATES = {"OBSERVED_FULL_FILL", "OBSERVED_PARTIAL_FILL"}
NO_FILL_STATES = {"OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"}


def _clip_probability(value):
    value = float(value)
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


class DecomposedActionValueModel:
    """Finite-action decomposed value challenger with temporal calibration."""

    def __init__(self, **base_kwargs):
        self.base_kwargs = dict(base_kwargs)
        self.base = da.DirectActionValueModel(**base_kwargs)
        self.fitted = False

    def __getattr__(self, name):
        base = self.__dict__.get("base")
        if base is None:
            raise AttributeError(name)
        return getattr(base, name)

    @staticmethod
    def _fill_target(record):
        state = str(record.get("target_state") or "")
        if state in FILL_STATES:
            return 1.0
        if state in NO_FILL_STATES:
            return 0.0
        raise ValueError("decomposed action requires observed fill state")

    def _iter_records(self, rows, markets=None, *, filled_only=False):
        for record in self.base._iter_training_actions(rows, markets=markets):
            state = str(record.get("target_state") or "")
            if state not in FILL_STATES | NO_FILL_STATES:
                continue
            if filled_only and state not in FILL_STATES:
                continue
            yield record

    def _factory(self, rows, markets=None, *, filled_only=False):
        return lambda: self._iter_records(
            rows, markets=markets, filled_only=filled_only)

    @staticmethod
    def _expected_value(fill_model, pnl_model, record):
        return (
            _clip_probability(fill_model.predict(record))
            * float(pnl_model.predict(record))
        )

    def _cell_multiplier(self, row, latency_ms):
        if not getattr(self.base, "conditional_calibration", False):
            return float(self.decomposed_calibration_multiplier)
        action = {
            "asset": str(row.get("asset") or "UNKNOWN"),
            "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
            "signal_age_ms": max(
                0.0, float(row.get("signal_age_ns") or 0) / 1e6),
            "latency_ms": int(latency_ms),
        }
        key = self.base._calibration_cell_from_action(action)
        cell = self.decomposed_conditional_multipliers.get(key)
        if isinstance(cell, dict) and da.finite(cell.get("multiplier")):
            return float(cell["multiplier"])
        return float(self.decomposed_calibration_multiplier)

    def fit(self, rows):
        rows = list(rows)
        if not rows:
            raise ValueError("decomposed action training rows required")

        # Fit the baseline object only for the common causal feature map,
        # support diagnostics, portfolio friction contract and safe geometry.
        self.base.fit(rows)
        markets = self.base._market_order(rows)
        if not markets:
            raise ValueError("no admissible decomposed-action markets")

        fit_markets = set(markets)
        scale_markets = set()
        calibration_markets = set()
        if len(markets) >= 15:
            fit_end = max(1, int(len(markets) * 0.60))
            scale_end = max(fit_end + 1, int(len(markets) * 0.80))
            scale_end = min(scale_end, len(markets) - 1)
            fit_markets = set(markets[:fit_end])
            scale_markets = set(markets[fit_end:scale_end])
            calibration_markets = set(markets[scale_end:])

        names = self.base.model_feature_names
        ridge = self.base.ridge
        batch = self.base.streaming_batch_size

        provisional_fill = da.StreamingRidge(
            names, ridge=ridge, batch_size=batch).fit_factory(
                self._factory(rows, fit_markets),
                self._fill_target,
            )
        provisional_pnl = da.StreamingRidge(
            names, ridge=ridge, batch_size=batch).fit_factory(
                self._factory(rows, fit_markets, filled_only=True),
                lambda record: float(record["target"]),
            )

        mean_fit_markets = fit_markets | scale_markets
        self.fill_model = da.StreamingRidge(
            names, ridge=ridge, batch_size=batch).fit_factory(
                self._factory(rows, mean_fit_markets),
                self._fill_target,
            )
        self.conditional_pnl_model = da.StreamingRidge(
            names, ridge=ridge, batch_size=batch).fit_factory(
                self._factory(rows, mean_fit_markets, filled_only=True),
                lambda record: float(record["target"]),
            )

        self.decomposed_scale_model = None
        self.decomposed_uncertainty_floor = 1e-6
        self.decomposed_calibration_multiplier = 1.5
        self.decomposed_conditional_multipliers = {}
        calibration_state = "INSUFFICIENT_MARKET_BLOCKS"
        calibration_market_scores = 0

        if scale_markets:
            self.decomposed_scale_model = da.StreamingRidge(
                names, ridge=ridge, batch_size=batch).fit_factory(
                    self._factory(rows, scale_markets),
                    lambda record: abs(
                        float(record["target"])
                        - self._expected_value(
                            provisional_fill, provisional_pnl, record)
                    ),
                )
            self.decomposed_uncertainty_floor = max(
                1e-6, 0.10 * self.decomposed_scale_model.target_mean)

        if calibration_markets and self.decomposed_scale_model is not None:
            market_scores = {}
            cell_scores = defaultdict(dict)
            for record in self._iter_records(rows, calibration_markets):
                predicted = self._expected_value(
                    self.fill_model, self.conditional_pnl_model, record)
                scale = max(
                    self.decomposed_uncertainty_floor,
                    float(self.decomposed_scale_model.predict(record)),
                )
                score = abs(float(record["target"]) - predicted) / scale
                market = str(record["market_id"])
                market_scores[market] = max(
                    float(score), market_scores.get(market, 0.0))
                cell = self.base._calibration_cell_from_action(record)
                cell_scores[cell][market] = max(
                    float(score), cell_scores[cell].get(market, 0.0))

            calibration_market_scores = len(market_scores)
            multiplier = da._quantile(
                list(market_scores.values()), self.base.calibration_level)
            if multiplier is not None and da.finite(multiplier):
                self.decomposed_calibration_multiplier = max(
                    1.0, float(multiplier))
                calibration_state = "TEMPORAL_MARKET_BLOCK_SPLIT_CONFORMAL"

                if self.base.conditional_calibration:
                    shrink = float(
                        self.base.conditional_calibration_shrinkage)
                    minimum = int(
                        self.base.conditional_calibration_min_markets)
                    for cell, scores in sorted(cell_scores.items()):
                        values = list(scores.values())
                        raw = da._quantile(
                            values, self.base.calibration_level)
                        if (
                            raw is None
                            or not da.finite(raw)
                            or len(values) < minimum
                        ):
                            continue
                        weight = (
                            len(values) / (len(values) + shrink)
                            if shrink else 1.0
                        )
                        value = max(
                            1.0,
                            weight * float(raw)
                            + (1.0 - weight)
                            * self.decomposed_calibration_multiplier,
                        )
                        self.decomposed_conditional_multipliers[cell] = {
                            "multiplier": float(value),
                            "raw_multiplier": float(max(1.0, raw)),
                            "markets": len(values),
                            "shrinkage_weight": float(weight),
                        }
            else:
                calibration_state = (
                    "INSUFFICIENT_CALIBRATION_ACTION_TARGETS")

        if self.decomposed_scale_model is None:
            # Conservative small-sample fallback: use the observed conditional
            # cash-PnL dispersion, never an invented zero uncertainty.
            self.decomposed_uncertainty_floor = max(
                1e-6, float(self.conditional_pnl_model.target_std))

        self.decomposed_selection_optimism_penalty = float(
            getattr(self.base, "selection_optimism_penalty", 0.0))

        base_receipt = dict(self.base.training_receipt)
        self.training_receipt = {
            **base_receipt,
            "schema": "polymarket_decomposed_direct_action_value_v1_training",
            "model": "LINEAR_FILL_PROBABILITY_X_CONDITIONAL_EXECUTABLE_CASH_PNL",
            "decomposed_research_only": True,
            "automatic_promotion": False,
            "fill_head_rows": int(self.fill_model.rows),
            "conditional_pnl_head_rows": int(
                self.conditional_pnl_model.rows),
            "fill_head_condition_number": float(
                self.fill_model.condition_number),
            "conditional_pnl_head_condition_number": float(
                self.conditional_pnl_model.condition_number),
            "decomposed_uncertainty_floor": float(
                self.decomposed_uncertainty_floor),
            "decomposed_calibration_multiplier": float(
                self.decomposed_calibration_multiplier),
            "decomposed_calibration_state": calibration_state,
            "decomposed_calibration_market_scores": int(
                calibration_market_scores),
            "decomposed_conditional_multipliers": dict(
                sorted(self.decomposed_conditional_multipliers.items())),
            "decomposed_selection_penalty": float(
                self.decomposed_selection_optimism_penalty),
            "decomposed_selection_penalty_source": (
                "BASELINE_PREQUENTIAL_ONE_SIDED_CONSERVATIVE_FALLBACK"
            ),
            "decomposition_semantics": (
                "P_ANY_FILL_TIMES_CONDITIONAL_TOTAL_EXECUTABLE_CASH_PNL;"
                "NO_FILL_VALUE_ZERO;PARTIAL_FILL_INCLUDED_IN_CONDITIONAL_HEAD"
            ),
        }
        self.fitted = True
        return self

    def _score_quantity(
        self, row, *, size, horizon_ms, latency_ms, side,
        portfolio_state, capital_budget,
    ):
        if not self.fitted:
            raise RuntimeError("decomposed action model not fitted")
        action = self.base._action_record(
            row, size=size, horizon_ms=horizon_ms,
            latency_ms=latency_ms, side=side)
        fill_probability = _clip_probability(
            self.fill_model.predict(action))
        conditional_cash = float(
            self.conditional_pnl_model.predict(action))
        mean = fill_probability * conditional_cash

        if self.decomposed_scale_model is None:
            scale = float(self.decomposed_uncertainty_floor)
        else:
            scale = max(
                float(self.decomposed_uncertainty_floor),
                float(self.decomposed_scale_model.predict(action)),
            )
        multiplier = self._cell_multiplier(row, latency_ms)
        uncertainty_penalty = (
            float(self.base.friction_policy.uncertainty_aversion)
            * multiplier * scale
        )
        selection_penalty = float(
            self.decomposed_selection_optimism_penalty)

        side_state = da.decision_side_state(row, side)
        notional = float(size) * float(side_state["ask"])
        base = {
            "action": "TRADE",
            "side": str(side),
            "size": float(size),
            "exit_horizon_ms": int(horizon_ms),
            "latency_ms": int(latency_ms),
            "signal_age_ms": max(
                0.0, float(row.get("signal_age_ns") or 0) / 1e6),
            "effective_action_age_ms": (
                max(0.0, float(row.get("signal_age_ns") or 0) / 1e6)
                + float(latency_ms)
            ),
            "notional": float(notional),
        }
        residual = da.residual_policy_friction(
            base, row,
            portfolio_state=portfolio_state,
            capital_budget=capital_budget,
            friction_policy=self.base.friction_policy,
        )
        lower_cash = mean - uncertainty_penalty - selection_penalty

        support_probability = self.base._predict_evidence_support(
            row, horizon_ms=horizon_ms,
            latency_ms=latency_ms, side=side)
        support_worst_case = self.base._support_worst_case_pnl(
            row, size=size, side=side)
        support_penalty = 0.0
        if self.base.support_policy_mode == "ROBUST_WORST_CASE":
            if (
                support_probability is None
                or support_worst_case is None
            ):
                lower_cash = float("-inf")
            else:
                probability = min(
                    1.0, max(0.0, float(support_probability)))
                robust = (
                    probability * lower_cash
                    + (1.0 - probability) * support_worst_case
                )
                support_penalty = max(
                    0.0, lower_cash - min(lower_cash, robust))
                lower_cash = min(lower_cash, robust)

        utility = lower_cash - residual["total_residual_friction"]
        return {
            **base,
            "value_model": "DECOMPOSED_FILL_X_CONDITIONAL_CASH",
            "predicted_fill_probability": float(fill_probability),
            "predicted_cash_pnl_given_fill": float(conditional_cash),
            "predicted_total_net_cash_pnl": float(mean),
            "predicted_total_net_pnl": float(mean),
            "predicted_abs_error_scale": float(scale),
            "calibration_multiplier_used": float(multiplier),
            "uncertainty_penalty": float(uncertainty_penalty),
            "selection_optimism_penalty": float(selection_penalty),
            "evidence_support_probability": support_probability,
            "support_worst_case_pnl": support_worst_case,
            "support_robustness_penalty": float(support_penalty),
            "calibrated_lower_cash_value": float(lower_cash),
            "calibrated_lower_value": float(utility),
            "policy_utility": float(utility),
            "policy_loss": float(-utility),
            "predicted_return_on_notional": (
                mean / notional if notional > 0 else None),
            **residual,
        }

    def _valid_live_geometry(self, row):
        if not da._valid_state(row):
            return False
        return (
            self.base.live_minimum_tte_ns
            <= int(row["tte_ns"])
            <= self.base.live_maximum_tte_ns
            and any(
                da.decision_side_state(row, side) is not None
                and float(
                    da.decision_side_state(row, side)["ask"])
                <= self.base.entry_cap + 1e-12
                for side in da.decision_action_sides(row)
            )
        )

    def score_actions(
        self, row, *, latency_ms=50, available_capital=None,
        live_geometry=True, portfolio_state=None,
        capital_budget=10_000.0,
    ):
        if not self.fitted:
            raise RuntimeError("decomposed action model not fitted")
        latency_ms = int(latency_ms)
        if latency_ms not in self.base.train_latencies_ms:
            return [], "LATENCY_OUTSIDE_TRAINING_SUPPORT"
        if not da._valid_state(row):
            return [], "STATE_OUTSIDE_RESEARCH_SUPPORT"
        if live_geometry and not self._valid_live_geometry(row):
            return [], "OUTSIDE_LIVE_GEOMETRY"

        scored = []
        for side in da.decision_action_sides(row):
            quantities = da.candidate_sizes(
                row,
                side=side,
                size_grid=self.base.size_grid,
                hard_order_notional=self.base.hard_order_notional,
                available_capital=available_capital,
                max_sizes=max(5, self.base.max_sizes_per_state),
            )
            if not quantities:
                continue
            for horizon in self.base.action_horizons_ms:
                if int(horizon) <= latency_ms:
                    continue
                for quantity in quantities:
                    scored.append(self._score_quantity(
                        row,
                        size=quantity,
                        horizon_ms=horizon,
                        latency_ms=latency_ms,
                        side=side,
                        portfolio_state=portfolio_state,
                        capital_budget=capital_budget,
                    ))
        scored.sort(
            key=lambda value: (
                value["policy_utility"],
                value["predicted_total_net_cash_pnl"],
                -value["notional"],
            ),
            reverse=True,
        )
        return (scored, "READY") if scored else (
            [], "NO_FEASIBLE_ACTION")

    def select_action(
        self, row, *, latency_ms=50, available_capital=None,
        live_geometry=True, minimum_lower_value=0.0,
        portfolio_state=None, capital_budget=10_000.0,
    ):
        scored, state = self.score_actions(
            row,
            latency_ms=latency_ms,
            available_capital=available_capital,
            live_geometry=live_geometry,
            portfolio_state=portfolio_state,
            capital_budget=capital_budget,
        )
        if (
            not scored
            or scored[0]["calibrated_lower_value"]
            <= float(minimum_lower_value)
        ):
            return {
                "action": "NO_TRADE",
                "reason": (
                    state if not scored
                    else "DECOMPOSED_LOWER_VALUE_NONPOSITIVE"),
                "latency_ms": int(latency_ms),
                "calibrated_lower_value": 0.0,
                "predicted_total_net_pnl": 0.0,
                "policy_utility": 0.0,
                "policy_loss": 0.0,
            }
        selected = dict(scored[0])
        selected["reason"] = "DECOMPOSED_VALUE_MAXIMUM"
        return selected

    def select_action_edge_sized(
        self, row, *, latency_ms=50, available_capital=None,
        live_geometry=True, minimum_lower_value=0.0,
        portfolio_state=None, capital_budget=10_000.0,
        sizing_policy=da.DEFAULT_EDGE_SIZING_POLICY,
    ):
        policy = sizing_policy.validated()
        if not self.fitted:
            raise RuntimeError("decomposed action model not fitted")
        if live_geometry and not self._valid_live_geometry(row):
            return {
                "action": "NO_TRADE",
                "reason": "OUTSIDE_LIVE_GEOMETRY",
                "latency_ms": int(latency_ms),
                "calibrated_lower_value": 0.0,
                "predicted_total_net_pnl": 0.0,
                "policy_utility": 0.0,
                "policy_loss": 0.0,
                "sizing_mode": "DECOMPOSED_EDGE_CONTEXT_BUDGET",
            }

        candidates = []
        for side in da.decision_action_sides(row):
            lower, upper = self.base._quantity_bounds(
                row, available_capital, side)
            if upper + 1e-12 < lower or upper <= 0:
                continue
            side_state = da.decision_side_state(row, side)
            if side_state is None:
                continue
            ask = float(side_state["ask"])
            for horizon in self.base.action_horizons_ms:
                if int(horizon) <= int(latency_ms):
                    continue
                probe = self._score_quantity(
                    row,
                    size=lower,
                    horizon_ms=horizon,
                    latency_ms=latency_ms,
                    side=side,
                    portfolio_state=portfolio_state,
                    capital_budget=capital_budget,
                )
                if (
                    probe["calibrated_lower_value"]
                    <= float(minimum_lower_value)
                ):
                    continue
                probe_notional = max(
                    1e-12, float(probe["notional"]))
                raw_edge = (
                    float(probe["calibrated_lower_value"])
                    / probe_notional
                )
                support = probe.get(
                    "evidence_support_probability")
                support_weight = (
                    min(1.0, max(0.0, float(support)))
                    if da.finite(support) else 0.0
                )
                edge = raw_edge * support_weight
                if edge <= 0:
                    continue
                desired = policy.desired_notional(
                    edge, capital_budget)
                if available_capital is not None:
                    desired = min(
                        desired, max(
                            0.0, float(available_capital)))
                desired = min(
                    desired,
                    self.base.hard_order_notional,
                    upper * ask,
                )
                quantity = min(
                    upper, max(lower, desired / ask))
                final = self._score_quantity(
                    row,
                    size=quantity,
                    horizon_ms=horizon,
                    latency_ms=latency_ms,
                    side=side,
                    portfolio_state=portfolio_state,
                    capital_budget=capital_budget,
                )
                if (
                    final["calibrated_lower_value"]
                    <= float(minimum_lower_value)
                ):
                    continue
                final = dict(final)
                final.update({
                    "reason": (
                        "DECOMPOSED_EDGE_CONTEXT_BUDGET_SIZING"),
                    "sizing_mode": (
                        "DECOMPOSED_EDGE_CONTEXT_BUDGET"),
                    "raw_admission_edge_per_dollar": float(
                        raw_edge),
                    "admission_edge_per_dollar": float(edge),
                    "sizing_support_probability": (
                        float(support)
                        if da.finite(support) else None),
                    "desired_notional_before_constraints": float(
                        policy.desired_notional(
                            edge, capital_budget)),
                    "desired_notional_after_constraints": float(
                        desired),
                    "context_budget": (
                        float(capital_budget)
                        / policy.context_count),
                    "context_capital_fraction": float(
                        policy.capital_fraction(edge)),
                })
                candidates.append(final)

        if not candidates:
            return {
                "action": "NO_TRADE",
                "reason": (
                    "NO_POSITIVE_DECOMPOSED_EDGE_AFTER_RESIZING"),
                "latency_ms": int(latency_ms),
                "calibrated_lower_value": 0.0,
                "predicted_total_net_pnl": 0.0,
                "policy_utility": 0.0,
                "policy_loss": 0.0,
                "sizing_mode": (
                    "DECOMPOSED_EDGE_CONTEXT_BUDGET"),
            }
        candidates.sort(
            key=lambda value: (
                value["policy_utility"],
                value["predicted_total_net_cash_pnl"],
                value["notional"],
            ),
            reverse=True,
        )
        return candidates[0]
