"""Separate action-conditional execution and joint net-value models.

Labels require post-decision arrival coverage. Censoring is not a non-fill.
The value model learns Q*Y jointly; it never assumes fill/payoff independence.
"""
from __future__ import annotations

from collections import Counter
import math
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from .models import Features
from .validation import weights


def execution_label(row, arrival):
    """Normalize an explicit, verified causal replay result (shares/USD)."""
    if not isinstance(arrival, dict) or arrival.get("coverage_verified") is not True:
        return {"state": "CENSORED", "quantity": None, "net_pnl": None, "reason": "POST_DECISION_COVERAGE_MISSING"}
    decision = row["decision_ns"]; start = arrival.get("arrival_ns"); end = arrival.get("information_ns")
    if (type(start) is not int or type(end) is not int or not decision < start <= end
            or arrival.get("decision_id") != row["decision_id"]
            or arrival.get("semantics") != "ARRIVAL_TOP_FAK_ACTUAL_PRICE_PARTIAL_FILL_V2"
            or not arrival.get("source_revisions")):
        raise ValueError("INVALID_ARRIVAL_EVIDENCE")
    fields = ("intended_quantity", "filled_quantity", "execution_price", "fees", "execution_cost", "slippage")
    for name in fields:
        v = arrival.get(name)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return {"state": "CENSORED", "quantity": None, "net_pnl": None, "reason": "EXECUTION_COST_OR_SIZE_UNKNOWN"}
    target = arrival["intended_quantity"]; q = arrival["filled_quantity"]; price = arrival["execution_price"]
    if not 0 <= q <= target or target <= 0 or not 0 <= price <= 1 or min(arrival["fees"], arrival["execution_cost"]) < 0:
        raise ValueError("INVALID_PARTIAL_FILL_ACCOUNTING")
    y = row.get("outcome")
    # Actual price already includes slippage. Do not subtract it twice.
    cost = q*price + arrival["fees"] + arrival["execution_cost"]
    return {"state": "NON_FILL" if q == 0 else "FILL" if q == target else "PARTIAL_FILL",
            "quantity": q, "intended_quantity": target, "execution_price": price, "fees": arrival["fees"],
            "execution_cost": arrival["execution_cost"], "slippage": arrival["slippage"],
            "cost": cost, "quantity_payoff": q*y if y is not None else None,
            "net_pnl": q*y-cost if y is not None else None,
            "information_ns": max(end, row.get("label_information_ns") or end),
            "time_to_fill_ns": start-decision if q else None,
            "action": arrival["action"], "source_revisions": arrival["source_revisions"],
            "accounting_scope": "RESEARCH_SIMULATED_EXECUTION_NEVER_CANONICAL_CASH"}


class ExecutionModel:
    def fit(self, rows, cutoff_ns, minimum_markets=40):
        self.cutoff_ns = cutoff_ns
        usable = [r for r in rows if isinstance(r.get("execution"), dict)
                  and r["execution"].get("net_pnl") is not None
                  and r["execution"].get("state") in {"FILL", "PARTIAL_FILL", "NON_FILL"}
                  and r["execution"]["information_ns"] < cutoff_ns]
        if len({r["market_id"] for r in usable}) < minimum_markets:
            raise ValueError("INSUFFICIENT_EXECUTION_EVIDENCE")
        self.features = Features().fit(usable); self.models = {}
        self.support = {}
        for action in sorted({r["execution"]["action"] for r in usable}):
            subset = [r for r in usable if r["execution"]["action"] == action]
            n = len({r["market_id"] for r in subset}); self.support[action] = n
            if n < minimum_markets:
                continue
            X = self.features.transform(subset); w = weights(subset)
            models = {}
            # Net value is estimated directly, retaining fill-selection dependence.
            for target in ("quantity", "quantity_payoff", "cost", "net_pnl"):
                model = HistGradientBoostingRegressor(max_leaf_nodes=5, max_iter=40,
                    l2_regularization=10, early_stopping=False, random_state=20260920)
                model.fit(X, [r["execution"][target] for r in subset], sample_weight=w)
                models[target] = model
            self.models[action] = models
        if not self.models:
            raise ValueError("INSUFFICIENT_ACTION_SUPPORT")
        return self

    def predict(self, rows, action, *, uncertainty_reserve):
        if action not in self.models or uncertainty_reserve is None or uncertainty_reserve < 0:
            raise ValueError("UNSUPPORTED_ACTION_OR_UNMEASURED_RESERVE")
        if any(r["decision_ns"] < self.cutoff_ns for r in rows):
            raise ValueError("EXECUTION_PREDICTION_BEFORE_FIT")
        X = self.features.transform(rows)
        values = {k: m.predict(X) for k, m in self.models[action].items()}
        values["conservative_ev"] = values["net_pnl"]-uncertainty_reserve
        return values


def economics(rows, take):
    chosen = [r for r, accept in zip(rows, take) if accept]
    completed = [r for r in chosen if (r.get("execution") or {}).get("net_pnl") is not None]
    values = [r["execution"] for r in completed]
    if len(completed) != len(chosen) or not values:
        return {"state": "INSUFFICIENT_EXECUTION_EVIDENCE", "actions": len(chosen),
                "observed_actions": len(completed), "censored": len(chosen)-len(completed),
                "net_pnl": None, "sharpe": None}
    pnl = np.asarray([x["net_pnl"] for x in values]); curve = np.r_[0., np.cumsum(pnl)]
    turnover = sum(x["quantity"]*x["execution_price"] for x in values)
    return {"state": "OBSERVED_REPLAY_ONLY", "actions": len(chosen), "markets": len({r["market_id"] for r in completed}),
            "net_pnl": float(pnl.sum()), "pnl_per_action": float(pnl.mean()),
            "return_on_turnover": float(pnl.sum()/turnover) if turnover > 0 else None,
            "drawdown": float(np.max(np.maximum.accumulate(curve)-curve)),
            "fill_fraction": sum(x["quantity"] > 0 for x in values)/len(values),
            "partial_fill_fraction": sum(x["state"] == "PARTIAL_FILL" for x in values)/len(values),
            "fees": sum(x["fees"] for x in values), "slippage": sum(x["slippage"] for x in values),
            "sharpe": None, "sharpe_reason": "NO_INDEPENDENT_REGULAR_CAPITAL_RETURN_SERIES",
            "canonical_pnl": False}


def compare_policies(train, calibration, audit, probabilities, *, train_cutoff, audit_cutoff,
                     threshold=.02):
    """Frozen chronological action comparison; one first action per market.

    LCB reserve is a calibration-period absolute prediction-error quantile,
    explicitly diagnostic rather than certified conditional coverage.
    Portfolio-wide capital replay remains an independent promotion gate.
    """
    masks = {k: [] for k in ("current_baseline", "pm_only", "raw_shock", "probability_only",
                             "probability_execution", "uncertainty_adjusted")}
    blocked = None; model = None; reserve = None
    try:
        model = ExecutionModel().fit(train, train_cutoff)
        errors = []
        for row in calibration:
            e = row.get("execution") or {}; action = e.get("action")
            if e.get("net_pnl") is None or action not in model.models or e["information_ns"] >= audit_cutoff:
                continue
            pred = model.predict([row], action, uncertainty_reserve=0.)["net_pnl"][0]
            errors.append(abs(pred-e["net_pnl"]))
        if len(errors) < 20:
            raise ValueError("INSUFFICIENT_EXECUTION_CALIBRATION")
        reserve = float(np.quantile(errors, .95))
    except ValueError as exc:
        blocked = str(exc)
    for r, p in zip(audit, probabilities):
        f = r["features"]; e = r.get("execution") or {}
        ask = f.get("ask"); fees = r.get("fee_per_share_at_decision")
        supported = ask is not None and fees is not None
        masks["current_baseline"].append(r.get("accepted") is True)
        masks["pm_only"].append(supported and r["pm_probability"]-ask-fees > threshold)
        shock = f.get("trigger_return_bp")
        masks["raw_shock"].append(shock is not None and abs(shock) >= .30 and
                                  f.get("window_distance_seconds") == 0)
        masks["probability_only"].append(supported and p-ask-fees > threshold)
        net = None
        if model and reserve is not None and e.get("action") in model.models:
            # Action metadata defines what was evaluated; future execution values
            # are never features of this forecast.
            net = float(model.predict([r], e["action"], uncertainty_reserve=reserve)["net_pnl"][0])
        masks["probability_execution"].append(net is not None and net > threshold)
        masks["uncertainty_adjusted"].append(net is not None and net-reserve > threshold)
    results = {}
    for name, mask in masks.items():
        used = set(); unique = []
        for r, take in zip(audit, mask):
            chosen = take and r["market_id"] not in used
            if chosen:
                used.add(r["market_id"])
            unique.append(chosen)
        results[name] = economics(audit, unique)
    return {"policies": results, "execution_model_blocker": blocked, "reserve": reserve,
            "reserve_semantics": "CALIBRATION_ABSOLUTE_ERROR_95_PERCENTILE_NOT_COVERAGE_CERTIFIED",
            "portfolio_capital_replay_verified": False,
            "counterfactual_transportability_validated": False}
