"""Run nested empirical Direct Action risk research from London PAPER evidence."""
from __future__ import annotations

import argparse
from pathlib import Path

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset
from research.walk_forward_v3.risk_frontier import nested_walk_forward_risk_frontier

SCHEMA = "polymarket_direct_action_empirical_risk_london_v1"


def run(
    root: Path,
    *,
    minimum_wall_ns: int,
    outer_folds: int = 3,
    inner_folds: int = 2,
    latency_ms: int = 50,
    capital_budget: float = 10_000.0,
):
    data = build_dataset(root, minimum_wall_ns=int(minimum_wall_ns))
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "input_state": data.get("input_state"),
        "data_sha256": data.get("data_sha256"),
        "minimum_wall_ns": int(minimum_wall_ns),
        "outer_folds": int(outer_folds),
        "inner_folds": int(inner_folds),
        "latency_ms": int(latency_ms),
        "capital_budget": float(capital_budget),
        "risk_budget": None,
        "risk_budget_semantics": (
            "NO_HUMAN_RISK_BUDGET_SUPPLIED_NO_SINGLE_POLICY_SELECTION"
        ),
        "mean_covariance_estimation": False,
        "gaussian_risk_assumption": False,
        "nested_frontier": None,
    }
    if data.get("input_state") != "READY":
        result["state"] = data.get("input_state")
        return result

    frontier = nested_walk_forward_risk_frontier(
        data["decisions"],
        desired_folds=int(outer_folds),
        inner_desired_folds=int(inner_folds),
        latency_ms=int(latency_ms),
        capital_budget=float(capital_budget),
        max_cvar95=None,
        max_drawdown=None,
    )
    result["nested_frontier"] = frontier
    result["state"] = frontier.get("state")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-wall-ns", type=int, required=True)
    parser.add_argument("--outer-folds", type=int, default=3)
    parser.add_argument("--inner-folds", type=int, default=2)
    parser.add_argument("--latency-ms", type=int, default=50)
    parser.add_argument("--capital-budget", type=float, default=10_000.0)
    args = parser.parse_args(argv)
    result = run(
        args.input_root,
        minimum_wall_ns=args.minimum_wall_ns,
        outer_folds=args.outer_folds,
        inner_folds=args.inner_folds,
        latency_ms=args.latency_ms,
        capital_budget=args.capital_budget,
    )
    atomic_json(args.output, result)
    return 0 if result.get("state") == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
