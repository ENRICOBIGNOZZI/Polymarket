#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Any

try:
    import v6_micro_taker_v3 as base
except ModuleNotFoundError:
    from scripts import v6_micro_taker_v3 as base

FEATURE_VERSION = 4
FEATURE_COUNT = 20


def _trade_momentum(flow: Any, token: str, mid: float, spread: float, lookback_seconds: int = 120) -> float:
    cutoff = int(flow.now) - max(1, int(lookback_seconds))
    rows = [trade for trade in flow.by_asset.get(token, []) if trade.ts >= cutoff]
    if len(rows) < 2 or spread <= 1e-9:
        return 0.0
    return max(-3.0, min(3.0, (rows[-1].price - rows[0].price) / spread))


def features(y: Any, n: Any, flow: Any, flow_window: int):
    raw = base.BASE_FEATURES(y, n, flow, flow_window)
    if raw is None:
        return None
    core, mid, spread = raw
    x14 = base.augment_vector(list(core), mid)
    yes_fast = flow.signed_flow(y.token, lookback_seconds=30)
    yes_slow = flow.signed_flow(y.token, lookback_seconds=600)
    no_fast = -flow.signed_flow(n.token, lookback_seconds=30)
    no_slow = -flow.signed_flow(n.token, lookback_seconds=600)
    yes_mom = _trade_momentum(flow, y.token, mid, spread, 120)
    no_mom = -_trade_momentum(flow, n.token, 1.0 - mid, spread, 120)
    return (
        x14 + [
            max(-1.0, min(1.0, yes_fast - yes_slow)),
            max(-1.0, min(1.0, no_fast - no_slow)),
            max(-1.0, min(1.0, 0.5 * (yes_fast + no_fast))),
            max(-1.0, min(1.0, 0.5 * (yes_slow + no_slow))),
            yes_mom,
            no_mom,
        ],
        mid,
        spread,
    )


def main() -> int:
    base.FEATURE_VERSION = FEATURE_VERSION
    base.FEATURE_COUNT = FEATURE_COUNT
    base.features = features
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
