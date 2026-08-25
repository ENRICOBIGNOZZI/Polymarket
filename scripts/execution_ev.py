#!/usr/bin/env python3
"""Canonical, joint-fill execution EV for a paper order or basket.

For a multi-leg candidate, the probability of a useful execution is a property
of the basket as a whole.  This module intentionally rejects a product of
marginal leg fill probabilities: it is not evidence of a joint fill.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_execution_ev_v1"


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class JointCompletionDistribution:
    """One empirical distribution for a complete basket, not its legs."""

    full: float
    partial: float
    zero: float
    source: str
    observations: int = 0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "JointCompletionDistribution":
        full = finite(raw.get("full"))
        partial = finite(raw.get("partial"))
        zero = finite(raw.get("zero"))
        source = str(raw.get("source") or "").strip()
        observations = int(finite(raw.get("observations")) or 0)
        if full is None or partial is None or zero is None:
            raise ValueError("joint completion distribution is incomplete")
        if min(full, partial, zero) < 0.0:
            raise ValueError("joint completion probabilities must be non-negative")
        if abs(full + partial + zero - 1.0) > 1e-8:
            raise ValueError("joint completion probabilities must sum to one")
        if not source:
            raise ValueError("joint completion source is required")
        forbidden = {"marginal_product", "product_of_marginals", "independence"}
        if source.lower() in forbidden:
            raise ValueError("product of marginal leg probabilities is forbidden")
        return cls(full, partial, zero, source, max(0, observations))


def expected_execution_value(
    *,
    completion: JointCompletionDistribution,
    conditional_alpha_usd: float,
    conditional_costs_usd: float,
    conditional_adverse_markout_usd: float,
    conditional_unwind_loss_usd: float,
    capital_latency_cost_usd: float,
) -> float:
    """Return conservative EV using the single basket completion distribution."""

    components = (
        conditional_alpha_usd,
        conditional_costs_usd,
        conditional_adverse_markout_usd,
        conditional_unwind_loss_usd,
        capital_latency_cost_usd,
    )
    if not all(math.isfinite(value) for value in components):
        raise ValueError("all EV components must be finite")
    if min(
        conditional_costs_usd,
        conditional_adverse_markout_usd,
        conditional_unwind_loss_usd,
        capital_latency_cost_usd,
    ) < 0.0:
        raise ValueError("cost and loss components must be non-negative")
    return (
        completion.full
        * (conditional_alpha_usd - conditional_costs_usd - conditional_adverse_markout_usd)
        - completion.partial * conditional_unwind_loss_usd
        - capital_latency_cost_usd
    )


def assess_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """Assess a candidate without routing it or changing any risk control."""

    reasons: list[str] = []
    leg_count = max(1, int(finite(raw.get("leg_count")) or 1))
    completion: JointCompletionDistribution | None = None
    distribution = raw.get("joint_completion")
    if not isinstance(distribution, dict):
        reasons.append("joint_completion_distribution_missing")
    else:
        try:
            completion = JointCompletionDistribution.from_mapping(distribution)
        except ValueError as exc:
            reasons.append(str(exc).replace(" ", "_"))

    if leg_count > 1 and raw.get("marginal_fill_probabilities") is not None:
        reasons.append("marginal_leg_probabilities_not_admissible")

    names = {
        "conditional_alpha_usd": "conditional_alpha_missing",
        "conditional_costs_usd": "conditional_costs_missing",
        "conditional_adverse_markout_usd": "conditional_adverse_markout_missing",
        "conditional_unwind_loss_usd": "conditional_unwind_loss_missing",
        "capital_latency_cost_usd": "capital_latency_cost_missing",
    }
    components: dict[str, float] = {}
    for name, reason in names.items():
        value = finite(raw.get(name))
        if value is None:
            reasons.append(reason)
        else:
            components[name] = value

    ev: float | None = None
    if completion is not None and not reasons:
        try:
            ev = expected_execution_value(completion=completion, **components)
        except ValueError as exc:
            reasons.append(str(exc).replace(" ", "_"))
    minimum = finite(raw.get("minimum_ev_usd"))
    minimum = 0.0 if minimum is None else minimum
    if ev is not None and ev <= minimum:
        reasons.append("non_positive_execution_ev")
    state = "ADMISSIBLE" if ev is not None and not reasons and ev > minimum else "INSUFFICIENT_EVIDENCE"
    return {
        "schema": SCHEMA,
        "candidate_id": str(raw.get("candidate_id") or ""),
        "leg_count": leg_count,
        "state": state,
        "admissible": state == "ADMISSIBLE",
        "expected_value_usd": ev,
        "minimum_ev_usd": minimum,
        "joint_completion": None
        if completion is None
        else {
            "full": completion.full,
            "partial": completion.partial,
            "zero": completion.zero,
            "source": completion.source,
            "observations": completion.observations,
        },
        "reason_codes": sorted(set(reasons)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="JSON candidate with joint completion inputs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    raw = json.loads(args.candidate.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("candidate must be a JSON object")
    result = assess_candidate(raw)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result["admissible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
