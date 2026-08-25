#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def full_kelly(q: float, p: float) -> float:
    if not (0.0 < p < 1.0):
        return 0.0
    return max(0.0, (q - p) / (1.0 - p))


def terminal_ev_per_share(terminal_probability: float, executable_price: float) -> float:
    return terminal_probability - executable_price


def horizon_semantics_fixture(
    *,
    current_mid: float = 0.50,
    one_step_fair: float = 0.55,
    executable_price: float = 0.51,
    terminal_probability: float = 0.50,
    fractional_kelly: float = 0.12,
    child_capital: float = 2500.0,
) -> dict[str, float | bool]:
    """Show that a one-step price forecast does not identify terminal binary EV.

    The incumbent PCA adjustment predicts the next standardized residual return,
    converts it into a nearby price-like `q`, and then the normal engine path uses
    that value as `fair_yes` for terminal binary Kelly sizing and resolved-outcome
    Brier scoring. This fixture intentionally keeps the one-step fair above the ask
    while the true terminal probability is below the ask.
    """

    gross_edge_interpreted_as_terminal = one_step_fair - executable_price
    terminal_ev = terminal_ev_per_share(terminal_probability, executable_price)
    kelly = full_kelly(one_step_fair, executable_price)
    kelly_dollars = fractional_kelly * kelly * child_capital
    return {
        "current_mid": current_mid,
        "one_step_fair": one_step_fair,
        "executable_price": executable_price,
        "terminal_probability": terminal_probability,
        "engine_interpreted_gross_edge": gross_edge_interpreted_as_terminal,
        "actual_terminal_ev_per_share": terminal_ev,
        "engine_full_kelly": kelly,
        "engine_fractional_kelly_fraction": fractional_kelly * kelly,
        "engine_fractional_kelly_dollars_before_other_caps": kelly_dollars,
        "sign_reversal": gross_edge_interpreted_as_terminal > 0.0 and terminal_ev < 0.0,
    }


def terminal_identification_set(
    one_step_fair: float = 0.55,
    executable_price: float = 0.51,
) -> list[dict[str, float]]:
    """Same one-step fair, different valid terminal probabilities and EV signs."""

    out: list[dict[str, float]] = []
    for terminal_probability in (0.40, 0.50, 0.60):
        out.append(
            {
                "one_step_fair": one_step_fair,
                "terminal_probability": terminal_probability,
                "terminal_ev_per_share": terminal_ev_per_share(terminal_probability, executable_price),
            }
        )
    return out


def source_contract(root: Path) -> dict[str, object]:
    engine = (root / "src" / "engine.cpp").read_text(encoding="utf-8")
    manager = (root / "scripts" / "multi_strategy_paper.py").read_text(encoding="utf-8")
    config = json.loads((root / "config" / "paper_v5.json").read_text(encoding="utf-8"))
    pca = next(item for item in config["multi_strategy"]["strategies"] if item["name"] == "pca")

    required_engine_fragments = [
        "next_resid.push_back(resid[t + 1][j]);",
        "The target is the next",
        "standardized idiosyncratic return conditional on the rolling residual state.",
        "const double forecast_std = y_mu + beta * x_now;",
        "const double q = logistic(logit(cur) + forecast_logit);",
        'out.push_back({"pca", it->second.first, it->second.second});',
        "s.fair_yes = fair;",
        "full_kelly(s.fair_side, s.executable_price)",
        "const double loss = (q - *m.resolved_yes) * (q - *m.resolved_yes);",
    ]
    manager_fragments = [
        'child["expert_weights"] = {',
        "name: (1.0 if name == strategy.expert else 0.0) for name in manager.expert_names",
    ]

    return {
        "engine_contract_present": all(fragment in engine for fragment in required_engine_fragments),
        "manager_singleton_contract_present": all(fragment in manager for fragment in manager_fragments),
        "required_engine_fragments": required_engine_fragments,
        "manager_fragments": manager_fragments,
        "pca_capital_fraction": float(pca["capital_fraction"]),
        "pca_interval_seconds": int(pca["overrides"]["interval_seconds"]),
        "pca_fractional_kelly": float(pca["overrides"]["fractional_kelly"]),
        "pca_max_trade_usd": float(pca["overrides"]["max_trade_usd"]),
        "parent_starting_capital": float(config["starting_capital"]),
        "pca_child_starting_capital": float(config["starting_capital"]) * float(pca["capital_fraction"]),
    }


def summarize(root: Path) -> dict[str, object]:
    contract = source_contract(root)
    fixture = horizon_semantics_fixture(
        fractional_kelly=float(contract["pca_fractional_kelly"]),
        child_capital=float(contract["pca_child_starting_capital"]),
    )
    return {
        "schema": "polymarket_lf_v5_pca_horizon_semantics_audit_v1",
        "source_contract": contract,
        "counterexample": fixture,
        "terminal_identification_set": terminal_identification_set(),
        "structural_finding": (
            "V5 PCA estimates a one-step standardized idiosyncratic return and turns that mark-to-market "
            "forecast into q_yes. The singleton PCA child then routes q_yes through the terminal-probability "
            "decision path, including binary Kelly sizing and eventual resolved-outcome Brier scoring. A "
            "one-step price forecast does not by itself identify E[terminal binary payout]."
        ),
        "interpretation": (
            "The deterministic sign-reversal fixture is a semantics/identification counterexample, not a live "
            "PnL estimate. It shows that positive edge under the engine's terminal interpretation can coexist "
            "with negative terminal expected value when the PCA output is only a short-horizon price forecast."
        ),
        "required_experiment": [
            "first use timestamp-aligned PCA histories so the target horizon is a real chronological interval",
            "evaluate PCA as a mark-to-market model on explicit 15m/30m/1h/6h future logit or midpoint targets using purged event/time-clustered OOS rows",
            "for mark-to-market trading, compare bounded cost-aware sizing and explicit exit-horizon utility without interpreting the forecast as a terminal binary probability",
            "if PCA state is to produce a terminal probability, fit a separate time-to-resolution decoder/calibration using only prior resolved events and evaluate Brier, log-loss and calibration by horizon",
            "compare incumbent mixed semantics against separated markout/terminal variants on identical chronological rows with 1x/1.5x/2x costs, turnover, capital occupancy, drawdown and realized OOS net PnL",
            "jointly control the existing PCA timestamp, residual-inference and singleton-confidence defects before attributing any economic difference to horizon semantics",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    print(json.dumps(summarize(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
