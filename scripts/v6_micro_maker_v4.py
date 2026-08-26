#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    import v6_micro_maker_v3 as v3
except ModuleNotFoundError:
    from scripts import v6_micro_maker_v3 as v3

v2 = v3.v2
base = v3.base


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _consume_float_arg(flag: str, default: float) -> float:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return default
    raw = sys.argv[i + 1] if i + 1 < len(sys.argv) else str(default)
    del sys.argv[i : min(len(sys.argv), i + 2)]
    return _finite(raw, default)


def _consume_int_arg(flag: str, default: int) -> int:
    return max(1, int(round(_consume_float_arg(flag, float(default)))))


def persistence_gated_fill_probability(
    raw_fill_probability: float,
    *,
    queue_ahead: float,
    burst_count: int,
    newest_event_age_seconds: float,
    min_inside_bursts: int,
    max_inside_event_age_seconds: float,
) -> float:
    """Suppress only inside-spread fill uplift unsupported by persistent fresh flow.

    The public-trade evidence is bursty. A couple of prints separated by only
    a few seconds are not enough to estimate a prospective fill hazard,
    especially when the newest market event is already stale by quote time.
    At-touch quotes retain the recurrence-shrunk queue model from
    ``v6_market_common``; this extra gate applies only when zero displayed queue
    identifies a price-improving quote.
    """
    raw = max(0.0, min(1.0, _finite(raw_fill_probability, 0.0)))
    if _finite(queue_ahead, 0.0) > 1e-12:
        return raw
    required_bursts = max(1, int(min_inside_bursts))
    age = _finite(newest_event_age_seconds, math.inf)
    max_age = max(0.0, _finite(max_inside_event_age_seconds, 0.0))
    if int(burst_count) < required_bursts:
        return 0.0
    if not math.isfinite(age) or age > max_age:
        return 0.0
    return raw


def install_flow_persistence_gate(
    *,
    min_inside_bursts: int,
    burst_gap_seconds: int,
    max_inside_event_age_seconds: float,
) -> dict[str, Any]:
    """Install persistence before V3 toxicity so both filters compose.

    The resulting V4 arm requires recurrence-shrunk causal flow, fresh
    multi-burst evidence for inside-spread improvement, and V3's directional
    toxicity/microstructure protection. The previous V4 arm omitted toxicity,
    which made it possible for improved fillability to reintroduce the same
    adverse-selection pattern already observed in filled paper windows.
    """
    original_tape_flow = base.TapeFlow
    original_fill_probability = base.fill_probability_proxy
    context: dict[str, Any] = {
        "burst_count": 0,
        "newest_event_age_seconds": math.inf,
        "evaluations": 0,
        "inside_blocks": 0,
        "inside_passes": 0,
        "max_burst_count": 0,
        "min_inside_bursts": max(1, int(min_inside_bursts)),
        "burst_gap_seconds": max(1, int(burst_gap_seconds)),
        "max_inside_event_age_seconds": max(0.0, float(max_inside_event_age_seconds)),
        "toxicity_composed": True,
    }

    class PersistentTapeFlow(original_tape_flow):
        def compatible_sell_rate(self, token_id: str, limit_price: float, *, lookback_seconds: int) -> float:
            rate = super().compatible_sell_rate(token_id, limit_price, lookback_seconds=lookback_seconds)
            bursts = self.compatible_sell_burst_count(
                token_id,
                limit_price,
                lookback_seconds=lookback_seconds,
                gap_seconds=context["burst_gap_seconds"],
            )
            age = self.compatible_sell_recency(
                token_id,
                limit_price,
                lookback_seconds=lookback_seconds,
            )
            context["burst_count"] = int(bursts)
            context["newest_event_age_seconds"] = float(age)
            context["max_burst_count"] = max(int(context["max_burst_count"]), int(bursts))
            return rate

    def gated_fill_probability(*args: Any, **kwargs: Any) -> float:
        raw = original_fill_probability(*args, **kwargs)
        queue_ahead = kwargs.get("queue_ahead")
        if queue_ahead is None and args:
            queue_ahead = args[0]
        queue = _finite(queue_ahead, 0.0)
        adjusted = persistence_gated_fill_probability(
            raw,
            queue_ahead=queue,
            burst_count=int(context["burst_count"]),
            newest_event_age_seconds=float(context["newest_event_age_seconds"]),
            min_inside_bursts=int(context["min_inside_bursts"]),
            max_inside_event_age_seconds=float(context["max_inside_event_age_seconds"]),
        )
        context["evaluations"] = int(context["evaluations"]) + 1
        if queue <= 1e-12 and raw > 0.0:
            if adjusted <= 0.0:
                context["inside_blocks"] = int(context["inside_blocks"]) + 1
            else:
                context["inside_passes"] = int(context["inside_passes"]) + 1
        return adjusted

    base.TapeFlow = PersistentTapeFlow
    base.fill_probability_proxy = gated_fill_probability
    return context


def main() -> int:
    run_dir_text = v2._arg_value("--run-dir")
    min_inside_bursts = _consume_int_arg("--min-inside-flow-bursts", 3)
    burst_gap_seconds = _consume_int_arg("--inside-flow-burst-gap-seconds", 30)
    max_inside_event_age_seconds = _consume_float_arg("--max-inside-flow-event-age-seconds", 30.0)
    context = install_flow_persistence_gate(
        min_inside_bursts=min_inside_bursts,
        burst_gap_seconds=burst_gap_seconds,
        max_inside_event_age_seconds=max_inside_event_age_seconds,
    )

    # V3 installs the causal directional toxicity filter on top of this wrapper,
    # then V2 installs the inside-confidence gate. V3 also persists 45/60/300s
    # executable markouts under 1x/1.5x/2x slippage stress. The caller's TTL is
    # deliberately left unchanged: the latest tape suggests a longer-lived
    # at-touch quote could improve fillability, but extending TTL without
    # causal resting-order revalidation would expose a stale quote after its
    # original edge/toxicity state has changed.
    result = v3.main()
    if run_dir_text:
        payload = {
            "paper_only": True,
            "authenticated_execution": False,
            **context,
            "ttl_policy": "caller_unchanged_until_resting_order_revalidation",
            "toxicity_status_file": "toxicity_status.json",
            "markout_file": "maker_markouts.csv",
        }
        path = Path(run_dir_text) / "flow_persistence_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
