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


def mixed_horizon_window(
    *,
    window_returns: int = 720,
    bootstrap_fidelity_minutes: int = 30,
    live_interval_seconds: int = 15,
    live_elapsed_seconds: int = 3600,
) -> dict[str, float | int]:
    """Approximate how a price-only rolling window changes horizon after bootstrap.

    The engine keeps prices without timestamps in `history_`. A fresh bootstrap can
    contribute 30-minute observations while the V5 PCA child appends one live price
    per loop. Treating every adjacent pair as the same one-step return therefore
    makes the composition of the estimation horizon depend on runtime age. The live
    loop actually takes processing time plus its configured sleep, so this is a
    lower-bound cadence approximation rather than a wall-clock timing claim.
    """

    live_points = max(0, live_elapsed_seconds // max(1, live_interval_seconds))
    live_returns = min(window_returns, live_points)
    bootstrap_returns = max(0, window_returns - live_returns)
    return {
        "window_returns": window_returns,
        "bootstrap_fidelity_seconds": bootstrap_fidelity_minutes * 60,
        "configured_live_sleep_seconds": live_interval_seconds,
        "live_elapsed_seconds": live_elapsed_seconds,
        "approx_live_returns": live_returns,
        "approx_bootstrap_returns": bootstrap_returns,
        "approx_live_fraction": live_returns / window_returns if window_returns else 0.0,
        "approx_bootstrap_fraction": bootstrap_returns / window_returns if window_returns else 0.0,
    }


def source_contract(root: Path) -> dict[str, object]:
    engine = (root / "src" / "engine.cpp").read_text(encoding="utf-8")
    engine_header = (root / "include" / "pm" / "engine.hpp").read_text(encoding="utf-8")
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
        "d.push_back(std::stod(x[2]));",
        "append_history(ts, m, mid);",
    ]
    manager_fragments = [
        'child["expert_weights"] = {',
        "name: (1.0 if name == strategy.expert else 0.0) for name in manager.expert_names",
    ]
    header_fragments = [
        "std::unordered_map<std::string,std::deque<double>> history_;",
    ]

    return {
        "engine_contract_present": all(fragment in engine for fragment in required_engine_fragments),
        "history_is_price_only_contract_present": all(fragment in engine_header for fragment in header_fragments),
        "manager_singleton_contract_present": all(fragment in manager for fragment in manager_fragments),
        "required_engine_fragments": required_engine_fragments,
        "header_fragments": header_fragments,
        "manager_fragments": manager_fragments,
        "pca_capital_fraction": float(pca["capital_fraction"]),
        "pca_interval_seconds": int(pca["overrides"]["interval_seconds"]),
        "pca_fractional_kelly": float(pca["overrides"]["fractional_kelly"]),
        "pca_max_trade_usd": float(pca["overrides"]["max_trade_usd"]),
        "pca_window": int(pca["overrides"]["pca_window"]),
        "history_fidelity_minutes": int(config["history_fidelity_minutes"]),
        "parent_starting_capital": float(config["starting_capital"]),
        "pca_child_starting_capital": float(config["starting_capital"]) * float(pca["capital_fraction"]),
    }


def summarize(root: Path) -> dict[str, object]:
    contract = source_contract(root)
    fixture = horizon_semantics_fixture(
        fractional_kelly=float(contract["pca_fractional_kelly"]),
        child_capital=float(contract["pca_child_starting_capital"]),
    )
    horizon_mix = [
        mixed_horizon_window(
            window_returns=int(contract["pca_window"]),
            bootstrap_fidelity_minutes=int(contract["history_fidelity_minutes"]),
            live_interval_seconds=int(contract["pca_interval_seconds"]),
            live_elapsed_seconds=seconds,
        )
        for seconds in (0, 3600, 7200, 10800)
    ]
    return {
        "schema": "polymarket_lf_v5_pca_horizon_semantics_audit_v1",
        "source_contract": contract,
        "counterexample": fixture,
        "terminal_identification_set": terminal_identification_set(),
        "target_horizon_mix": horizon_mix,
        "structural_finding": (
            "V5 PCA estimates an index-step standardized idiosyncratic return and turns that mark-to-market "
            "forecast into q_yes. The singleton PCA child then routes q_yes through the terminal-probability "
            "decision path, including binary Kelly sizing and eventual resolved-outcome Brier scoring. A "
            "one-step price forecast does not by itself identify E[terminal binary payout]. Moreover history_ "
            "stores prices without timestamps, while bootstrap and live observations have radically different "
            "cadences, so even the duration represented by one index step is not stable without timestamp alignment."
        ),
        "interpretation": (
            "The deterministic sign-reversal fixture is a semantics/identification counterexample, not a live "
            "PnL estimate. The horizon-mix table is based on configured cadence and assumes one appended live "
            "observation per configured sleep; actual loops include processing time. Both diagnostics show why "
            "the PCA output needs an explicit target horizon before it can be assigned terminal probability semantics."
        ),
        "required_experiment": [
            "first use timestamp-aligned PCA histories so every target is attached to an explicit chronological horizon rather than an observation index",
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
