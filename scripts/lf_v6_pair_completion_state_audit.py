#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PairEconomics:
    p_a: float
    p_b: float
    completed_pnl: float
    a_only_pnl: float
    b_only_pnl: float


def frechet_joint_bounds(p_a: float, p_b: float) -> tuple[float, float]:
    if not 0.0 <= p_a <= 1.0 or not 0.0 <= p_b <= 1.0:
        raise ValueError("marginal fill probabilities must be in [0,1]")
    return max(0.0, p_a + p_b - 1.0), min(p_a, p_b)


def fill_state_probabilities(p_a: float, p_b: float, joint: float) -> dict[str, float]:
    lower, upper = frechet_joint_bounds(p_a, p_b)
    if joint < lower - 1e-12 or joint > upper + 1e-12:
        raise ValueError("joint completion is incompatible with the supplied marginals")
    states = {
        "both": joint,
        "a_only": p_a - joint,
        "b_only": p_b - joint,
        "none": 1.0 - p_a - p_b + joint,
    }
    if min(states.values()) < -1e-12:
        raise ValueError("invalid fill-state distribution")
    return {key: max(0.0, value) for key, value in states.items()}


def expected_pair_pnl(economics: PairEconomics, joint: float) -> float:
    states = fill_state_probabilities(economics.p_a, economics.p_b, joint)
    return (
        states["both"] * economics.completed_pnl
        + states["a_only"] * economics.a_only_pnl
        + states["b_only"] * economics.b_only_pnl
    )


def incumbent_completion_proxy(p_a: float, p_b: float) -> float:
    """The repaired LF V3 selection proxy: min(per-leg marginal fill probability).

    For two legs this equals the Frechet *upper bound* on true joint completion.
    It is therefore not a conservative joint-completion estimator.
    """
    return min(p_a, p_b)


def audit_case() -> dict[str, object]:
    economics = PairEconomics(
        p_a=0.10,
        p_b=0.10,
        completed_pnl=1.00,
        a_only_pnl=-0.30,
        b_only_pnl=-0.30,
    )
    lower, upper = frechet_joint_bounds(economics.p_a, economics.p_b)
    independent = economics.p_a * economics.p_b
    incumbent = incumbent_completion_proxy(economics.p_a, economics.p_b)
    scenarios = {
        "mutually_exclusive": lower,
        "independent": independent,
        "perfect_positive_dependence": upper,
    }
    return {
        "economics": asdict(economics),
        "joint_completion": {
            "frechet_lower": lower,
            "independence": independent,
            "incumbent_min_marginal_proxy": incumbent,
            "frechet_upper": upper,
            "incumbent_is_upper_bound": abs(incumbent - upper) <= 1e-12,
        },
        "scenarios": {
            name: {
                "joint_completion": joint,
                "state_probabilities": fill_state_probabilities(economics.p_a, economics.p_b, joint),
                "expected_pnl_per_window": expected_pair_pnl(economics, joint),
            }
            for name, joint in scenarios.items()
        },
        "interpretation": (
            "Identical per-leg fill marginals do not identify joint completion or pair EV. "
            "Using min(per-leg fill probability) treats the Frechet upper bound as the completion proxy "
            "and omits one-leg abort/unwind losses."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit LF pair completion dependence and partial-fill economics")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_case()
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
