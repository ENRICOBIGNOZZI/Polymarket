#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpacingCase:
    hourly_phi: float
    gap_hours: float
    assumed_fidelity_minutes: int
    estimated_one_step_phi: float
    true_half_life_hours: float
    modeled_half_life_hours: float
    modeled_hold_hours: float
    modeled_reversion_fraction: float
    true_reversion_fraction: float
    reversion_overstatement: float


def estimate_ar1_phi(levels: list[float]) -> float:
    if len(levels) < 3:
        raise ValueError("at least three levels are required")
    x = levels[:-1]
    y = levels[1:]
    xm = sum(x) / len(x)
    ym = sum(y) / len(y)
    sxx = sum((v - xm) ** 2 for v in x)
    if sxx <= 1e-15:
        raise ValueError("degenerate level path")
    return sum((a - xm) * (b - ym) for a, b in zip(x, y)) / sxx


def half_life_steps(phi: float) -> float:
    if not 0.0 < phi < 1.0:
        return math.inf
    return -math.log(2.0) / math.log(phi)


def deterministic_decay(hourly_phi: float, gap_hours: float, points: int = 36) -> list[float]:
    return [hourly_phi ** (gap_hours * i) for i in range(points)]


def spacing_case(hourly_phi: float, gap_hours: float, assumed_fidelity_minutes: int = 60) -> SpacingCase:
    levels = deterministic_decay(hourly_phi, gap_hours)
    estimated_phi = estimate_ar1_phi(levels)
    true_half_life = half_life_steps(hourly_phi)
    modeled_half_life = half_life_steps(estimated_phi) * assumed_fidelity_minutes / 60.0
    modeled_hold = 2.0 * modeled_half_life
    modeled_steps = modeled_hold * 60.0 / assumed_fidelity_minutes
    modeled_reversion = 1.0 - estimated_phi ** modeled_steps
    true_reversion = 1.0 - hourly_phi ** modeled_hold
    ratio = modeled_reversion / true_reversion if true_reversion > 0.0 else math.inf
    return SpacingCase(
        hourly_phi=hourly_phi,
        gap_hours=gap_hours,
        assumed_fidelity_minutes=assumed_fidelity_minutes,
        estimated_one_step_phi=estimated_phi,
        true_half_life_hours=true_half_life,
        modeled_half_life_hours=modeled_half_life,
        modeled_hold_hours=modeled_hold,
        modeled_reversion_fraction=modeled_reversion,
        true_reversion_fraction=true_reversion,
        reversion_overstatement=ratio,
    )


def source_contract(path: Path) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8")
    return {
        "intersects_timestamps": "times = sorted(common)" in text,
        "fits_ar_without_timestamps": "ar_fit(resid)" in text,
        "uses_configured_fidelity_for_hold": "args.fidelity_minutes" in text,
        "checks_adjacent_timestamp_spacing": (
            "times[i] - times[i - 1]" in text
            or "times[i]-times[i-1]" in text
            or "expected_gap" in text
        ),
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    regular = spacing_case(0.95, 1.0)
    sparse_6h = spacing_case(0.95, 6.0)
    sparse_12h = spacing_case(0.95, 12.0)
    payload = {
        "finding": "irregular_common_history_is_treated_as_regular_fidelity",
        "source_contract": source_contract(root / "scripts" / "v6_local_factor_intents.py"),
        "cases": {
            "regular_1h": asdict(regular),
            "sparse_6h": asdict(sparse_6h),
            "sparse_12h": asdict(sparse_12h),
        },
        "required_successor_contract": [
            "require a regular common timestamp grid or model elapsed time explicitly",
            "do not count a missing multi-bar gap as one AR transition",
            "estimate mean-reversion speed in wall-clock units",
            "forecast the hold horizon from actual elapsed time rather than row count",
            "treat a partial current bucket separately from a completed fidelity interval",
            "rerun common-sample threshold aggression only after time-spacing repair",
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
