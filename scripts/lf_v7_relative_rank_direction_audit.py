#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass


def clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def logit(p: float) -> float:
    p = clamp(float(p), 1e-9, 1.0 - 1e-9)
    return math.log(p / (1.0 - p))


def relative_targets(current_yes: list[float], future_yes: list[float]) -> tuple[list[float], list[float], float]:
    if len(current_yes) != len(future_yes) or not current_yes:
        raise ValueError("current/future cross-sections must be non-empty and aligned")
    moves = [logit(f) - logit(c) for c, f in zip(current_yes, future_yes)]
    mode = statistics.median(moves)
    return moves, [move - mode for move in moves], mode


def single_leg_markout(side: str, current_yes: float, future_yes: float) -> float:
    side = side.upper()
    if side == "YES":
        return future_yes - current_yes
    if side == "NO":
        return (1.0 - future_yes) - (1.0 - current_yes)
    raise ValueError("side must be YES or NO")


@dataclass(frozen=True)
class Counterexample:
    name: str
    current_yes: tuple[float, ...]
    future_yes: tuple[float, ...]
    absolute_logit_moves: tuple[float, ...]
    relative_targets: tuple[float, ...]
    cross_sectional_mode: float
    selected_market: int
    mapped_side: str
    single_leg_markout: float


def build_counterexamples() -> list[Counterexample]:
    examples: list[Counterexample] = []

    # All contracts rise in absolute terms. The weakest contract has a negative
    # relative target, but BUY NO still loses because its YES price rises.
    current = [0.50, 0.50, 0.50]
    future = [0.70, 0.65, 0.60]
    moves, rel, mode = relative_targets(current, future)
    idx = min(range(len(rel)), key=lambda i: rel[i])
    examples.append(
        Counterexample(
            name="negative_relative_but_positive_absolute_yes_move",
            current_yes=tuple(current),
            future_yes=tuple(future),
            absolute_logit_moves=tuple(moves),
            relative_targets=tuple(rel),
            cross_sectional_mode=mode,
            selected_market=idx,
            mapped_side="NO",
            single_leg_markout=single_leg_markout("NO", current[idx], future[idx]),
        )
    )

    # All contracts fall in absolute terms. The least-negative contract has a
    # positive relative target, but BUY YES still loses because its YES price falls.
    current = [0.50, 0.50, 0.50]
    future = [0.40, 0.35, 0.30]
    moves, rel, mode = relative_targets(current, future)
    idx = max(range(len(rel)), key=lambda i: rel[i])
    examples.append(
        Counterexample(
            name="positive_relative_but_negative_absolute_yes_move",
            current_yes=tuple(current),
            future_yes=tuple(future),
            absolute_logit_moves=tuple(moves),
            relative_targets=tuple(rel),
            cross_sectional_mode=mode,
            selected_market=idx,
            mapped_side="YES",
            single_leg_markout=single_leg_markout("YES", current[idx], future[idx]),
        )
    )
    return examples


def paired_relative_markout(current_yes: list[float], future_yes: list[float]) -> float:
    moves, rel, _mode = relative_targets(current_yes, future_yes)
    top = max(range(len(rel)), key=lambda i: rel[i])
    bottom = min(range(len(rel)), key=lambda i: rel[i])
    return single_leg_markout("YES", current_yes[top], future_yes[top]) + single_leg_markout(
        "NO", current_yes[bottom], future_yes[bottom]
    )


def audit_report() -> dict[str, object]:
    examples = build_counterexamples()
    paired_example = paired_relative_markout([0.50, 0.50, 0.50], [0.70, 0.65, 0.60])
    return {
        "research_state": "MORE_EVIDENCE_REQUIRED",
        "finding": "relative cross-sectional target sign does not identify absolute single-leg markout sign",
        "target_definition": "Delta logit_i - cross-sectional/group baseline",
        "incumbent_mapping_under_audit": "positive relative forecast -> BUY YES; negative relative forecast -> BUY NO",
        "counterexamples": [asdict(example) for example in examples],
        "paired_top_bottom_zero_cost_markout_example": paired_example,
        "implication": (
            "A relative ranking forecast can support a paired/portfolio relative-value trade, or a single-leg trade only after "
            "adding an independently estimated cross-sectional market-mode forecast. It cannot be interpreted directly as "
            "an absolute YES/NO markout forecast."
        ),
        "required_successor_contract": [
            "preserve the relative target for rank-IC/decile diagnostics",
            "for single-leg execution, reconstruct absolute expected logit move as relative forecast plus a separately estimated market/group mode forecast",
            "or execute top-vs-bottom relative portfolios with explicit joint fill and abort/unwind economics",
            "evaluate absolute directional hit rate and executable fill-conditioned PnL separately from relative rank IC",
            "retain authoritative fees, explicit bid/ask/slippage, 15% drawdown, kill switch and paper-only execution",
        ],
        "production_changed": False,
        "authenticated_execution": False,
    }


def main() -> int:
    print(json.dumps(audit_report(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
