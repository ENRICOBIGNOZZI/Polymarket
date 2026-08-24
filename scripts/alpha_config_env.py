#!/usr/bin/env python3
"""Emit validated champion alpha parameters as shell-safe assignments."""
from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from alpha_research import load_config


def emit(name: str, value: object) -> str:
    return f"{name}={shlex.quote(str(value))}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/alpha_research.json"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    b1 = cfg["_champions"]["B1"]
    b2 = cfg["_champions"]["B2"]

    values = {
        "B1_MARKETS": b1.params["markets"],
        "B1_HISTORY_UNIVERSE": b1.params["history_universe"],
        "B1_LOOKBACK_HOURS": b1.params["lookback_hours"],
        "B1_FIDELITY_MINUTES": b1.params["fidelity_minutes"],
        "B1_MIN_Z": b1.params["min_z"],
        "B1_MAX_HALF_LIFE_HOURS": b1.params["max_half_life_hours"],
        "B1_MIN_T_REVERSION": b1.params["min_t_reversion"],
        "B1_TOP": b1.params["top"],
        "B1_EXECUTION_MIN_EDGE": b1.execution_min_edge,
        "B2_MARKETS": b2.params["markets"],
        "B2_UNIVERSE": b2.params["universe"],
        "B2_LOOKBACK_HOURS": b2.params["lookback_hours"],
        "B2_FIDELITY_MINUTES": b2.params["fidelity_minutes"],
        "B2_FACTORS": b2.params["factors"],
        "B2_MAX_HEDGES": b2.params["max_hedges"],
        "B2_MIN_Z": b2.params["min_z"],
        "B2_MAX_HALF_LIFE_HOURS": b2.params["max_half_life_hours"],
        "B2_MIN_T_REVERSION": b2.params["min_t_reversion"],
        "B2_MAX_FACTOR_HEDGE_ERROR": b2.params["max_factor_hedge_error"],
        "B2_TOP": b2.params["top"],
        "B2_EXECUTION_MIN_EDGE": b2.execution_min_edge,
        "ALPHA_MERGE_MIN_EDGE": min(b1.execution_min_edge, b2.execution_min_edge),
    }
    for key, value in values.items():
        print(emit(key, value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
