#!/usr/bin/env python3
"""Strict markout wrapper for the forward maker shadow probe.

This module deliberately keeps the existing queue/fill replay unchanged while
repairing the markout observation contract used by research evidence:

* a horizon is observed only when a book snapshot at or after the exact horizon
  exists for the filled token;
* the final pre-horizon snapshot is never substituted for a censored horizon;
* a 45-second executable-bid markout is recorded alongside the existing 60s and
  300s horizons.

It remains read-only and delegates all order/fill economics to
``forward_maker_probe.py``.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_PATH = SCRIPT_DIR / "forward_maker_probe.py"
spec = importlib.util.spec_from_file_location("forward_maker_probe_base", BASE_PATH)
base = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = base
spec.loader.exec_module(base)

_original_simulate_leg = base.simulate_leg
_original_policy_result = base.policy_result


def strict_snapshot_after(
    snapshots: list[tuple[int, dict[str, base.Book]]],
    token: str,
    target_ts: int,
) -> base.Book | None:
    """Return the first token book observed at/after target_ts, else None."""
    for ts, books in snapshots:
        if ts >= target_ts and token in books:
            return books[token]
    return None


def _bid_markout(book: base.Book | None, limit_price: float) -> float | None:
    if book is None or not math.isfinite(book.best_bid):
        return None
    return book.best_bid - limit_price


def strict_simulate_leg(
    leg: base.QuoteLeg,
    trades: Iterable[base.Trade],
    snapshots: list[tuple[int, dict[str, base.Book]]],
    fee_rate: float = 0.0,
) -> base.LegReplay:
    """Run the canonical FIFO replay with strictly censored markout horizons."""
    previous = base.snapshot_after
    base.snapshot_after = strict_snapshot_after
    try:
        replay = _original_simulate_leg(leg, trades, snapshots, fee_rate)
    finally:
        base.snapshot_after = previous

    markout_45 = None
    if replay.first_fill_ts is not None:
        markout_45 = _bid_markout(
            strict_snapshot_after(snapshots, leg.token_id, replay.first_fill_ts + 45),
            leg.limit_price,
        )
    # LegReplay is intentionally mutable; the strict wrapper adds this research
    # field without changing the stable base schema consumed by old calibrators.
    setattr(replay, "markout_45_bid_per_share", markout_45)
    return replay


def strict_policy_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Preserve the base result schema and append the new 45s markout fields."""
    yes = kwargs.get("yes")
    no = kwargs.get("no")
    result = _original_policy_result(*args, **kwargs)
    if isinstance(result.get("yes"), dict):
        result["yes"]["markout_45_bid_per_share"] = getattr(
            yes, "markout_45_bid_per_share", None
        )
    if isinstance(result.get("no"), dict):
        result["no"]["markout_45_bid_per_share"] = getattr(
            no, "markout_45_bid_per_share", None
        )
    return result


def main() -> int:
    # main() resolves these functions from the base module globals at runtime.
    # Rebinding therefore applies the strict horizon contract to the full existing
    # CLI without duplicating the queue/fill implementation.
    base.snapshot_after = strict_snapshot_after
    base.simulate_leg = strict_simulate_leg
    base.policy_result = strict_policy_result
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
