#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Leg:
    mid: float
    yes_sd: float
    loading: float
    side: str = "YES"


def side_sign(side: str) -> float:
    value = side.upper()
    if value == "YES":
        return 1.0
    if value == "NO":
        return -1.0
    raise ValueError(f"unsupported side: {side}")


def standardized_loading_exposure(leg: Leg) -> float:
    return side_sign(leg.side) * leg.loading


def price_factor_delta(leg: Leg) -> float:
    """First-order side-price exposure to one unit of the standardized factor.

    The V6 local factor is estimated in standardized logit coordinates:
        z_i = loading_i * factor + residual_i.
    Therefore d logit(p_i) / d factor = yes_sd_i * loading_i, while
    d p_i / d logit(p_i) = p_i * (1-p_i). NO-price exposure has the opposite sign.
    """
    if not (0.0 < leg.mid < 1.0):
        raise ValueError("mid must be strictly inside (0,1)")
    if not math.isfinite(leg.yes_sd) or leg.yes_sd <= 0.0:
        raise ValueError("yes_sd must be finite and positive")
    if not math.isfinite(leg.loading):
        raise ValueError("loading must be finite")
    return side_sign(leg.side) * leg.mid * (1.0 - leg.mid) * leg.yes_sd * leg.loading


def incumbent_weight(a: Leg, b: Leg) -> float:
    ea = standardized_loading_exposure(a)
    eb = standardized_loading_exposure(b)
    if abs(eb) <= 1e-12 or ea * eb >= 0.0:
        raise ValueError("legs do not have opposite standardized factor exposure")
    return abs(ea / eb)


def price_delta_weight(a: Leg, b: Leg) -> float:
    ea = price_factor_delta(a)
    eb = price_factor_delta(b)
    if abs(eb) <= 1e-12 or ea * eb >= 0.0:
        raise ValueError("legs do not have opposite price factor exposure")
    return abs(ea / eb)


def residual_price_exposure(a: Leg, b: Leg, weight_b: float) -> float:
    return price_factor_delta(a) + weight_b * price_factor_delta(b)


def fixture(name: str, a: Leg, b: Leg) -> dict[str, float | str | dict[str, float | str]]:
    old_weight = incumbent_weight(a, b)
    delta_weight = price_delta_weight(a, b)
    old_residual = residual_price_exposure(a, b, old_weight)
    a_delta = price_factor_delta(a)
    corrected_residual = residual_price_exposure(a, b, delta_weight)
    return {
        "name": name,
        "leg_a": asdict(a),
        "leg_b": asdict(b),
        "incumbent_weight_b": old_weight,
        "price_delta_weight_b": delta_weight,
        "incumbent_residual_price_factor_exposure": old_residual,
        "incumbent_residual_fraction_of_leg_a": abs(old_residual) / max(abs(a_delta), 1e-12),
        "delta_aware_residual_price_factor_exposure": corrected_residual,
    }


def source_contract(path: Path) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8")
    return {
        "uses_loading_only_for_pair_weight": "weight_b = abs((sign_a * a.loading) / (sign_b * b.loading))" in text,
        "converts_residual_to_logit_units_with_yes_sd": "yes_logit_move = sig.expected_residual_change * sig.yes_sd" in text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V6 Local Factor hedge units")
    parser.add_argument("--source", type=Path, default=Path("scripts/v6_local_factor_intents.py"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    fixtures = [
        fixture(
            "different_logit_scales_and_price_deltas",
            Leg(mid=0.50, yes_sd=0.10, loading=1.0),
            Leg(mid=0.95, yes_sd=0.30, loading=-1.0),
        ),
        fixture(
            "extreme_probability_delta_compression",
            Leg(mid=0.50, yes_sd=0.10, loading=1.0),
            Leg(mid=0.99, yes_sd=0.30, loading=-1.0),
        ),
        fixture(
            "same_price_different_logit_scale",
            Leg(mid=0.50, yes_sd=0.10, loading=1.0),
            Leg(mid=0.50, yes_sd=0.50, loading=-1.0),
        ),
    ]
    payload = {
        "schema": "polymarket_lf_v6_factor_hedge_units_audit_v1",
        "finding": "standardized-loading weights do not neutralize first-order price PnL factor exposure",
        "source_contract": source_contract(args.source),
        "fixtures": fixtures,
        "required_common_sample_test": [
            "incumbent standardized-loading hedge weights",
            "logit-scale-aware weights using loading * yes_sd",
            "price-delta-aware weights using p*(1-p)*loading*yes_sd",
            "compare factor beta, turnover, executable completion, partial-fill unwind loss and fill-conditioned PnL",
        ],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
