#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def rolling_max_flow(rows: list[tuple[int, float]], window_ms: int) -> float:
    if not rows or window_ms <= 0:
        return 0.0
    ordered = sorted(rows)
    left = 0
    total = 0.0
    best = 0.0
    for right, (received_ms, size) in enumerate(ordered):
        total += max(0.0, size)
        cutoff = received_ms - window_ms
        while left <= right and ordered[left][0] < cutoff:
            total -= max(0.0, ordered[left][1])
            left += 1
        best = max(best, total)
    return best


def compatible_flow(
    tape_rows: list[dict[str, str]],
    *,
    token_id: str,
    limit_price: float,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for row in tape_rows:
        if str(row.get("asset_id") or row.get("token_id") or "") != token_id:
            continue
        if str(row.get("side") or "").upper() != "SELL":
            continue
        received_ms = integer(row.get("received_ms"), 0)
        if received_ms <= 0 or received_ms < start_ms or received_ms >= end_ms:
            continue
        price = finite(row.get("price"), math.nan)
        size = max(0.0, finite(row.get("size"), 0.0))
        if not math.isfinite(price) or price > limit_price + 1e-12 or size <= 0.0:
            continue
        out.append((received_ms, size))
    return out


def audit(
    legs_rows: list[dict[str, str]],
    tape_rows: list[dict[str, str]],
    *,
    lookback_seconds: int = 900,
    execution_window_seconds: int = 180,
) -> dict[str, Any]:
    lookback_ms = max(1, int(lookback_seconds)) * 1000
    execution_ms = max(1, int(execution_window_seconds)) * 1000
    bundles: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in legs_rows:
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id:
            bundles[bundle_id].append(row)

    diagnostics: list[dict[str, Any]] = []
    for bundle_id, legs in sorted(bundles.items()):
        leg_diags: list[dict[str, Any]] = []
        for leg in legs:
            token_id = str(leg.get("token_id") or "")
            arrival_ms = integer(leg.get("arrival_ms"), 0)
            queue_ahead = max(0.0, finite(leg.get("queue_ahead"), 0.0))
            target_shares = max(0.0, finite(leg.get("target_shares"), 0.0))
            limit_price = finite(leg.get("limit_price"), math.nan)
            required = queue_ahead + target_shares

            prior = compatible_flow(
                tape_rows,
                token_id=token_id,
                limit_price=limit_price,
                start_ms=max(0, arrival_ms - lookback_ms),
                end_ms=arrival_ms,
            )
            post = compatible_flow(
                tape_rows,
                token_id=token_id,
                limit_price=limit_price,
                start_ms=arrival_ms,
                end_ms=arrival_ms + execution_ms,
            )
            prior_total = sum(size for _, size in prior)
            prior_max_window = rolling_max_flow(prior, execution_ms)
            post_total = sum(size for _, size in post)
            capacity_ratio = prior_max_window / required if required > 1e-12 else 0.0
            realized_clearance_ratio = post_total / required if required > 1e-12 else 0.0

            leg_diags.append(
                {
                    "market_id": str(leg.get("market_id") or ""),
                    "token_id": token_id,
                    "limit_price": limit_price,
                    "arrival_ms": arrival_ms,
                    "queue_ahead": queue_ahead,
                    "target_shares": target_shares,
                    "required_compatible_sell_flow": required,
                    "prior_lookback_compatible_sell_flow": prior_total,
                    "prior_max_execution_window_flow": prior_max_window,
                    "prior_capacity_ratio": capacity_ratio,
                    "post_entry_execution_window_flow": post_total,
                    "realized_clearance_ratio": realized_clearance_ratio,
                }
            )

        min_capacity = min((finite(x["prior_capacity_ratio"], 0.0) for x in leg_diags), default=0.0)
        min_realized = min((finite(x["realized_clearance_ratio"], 0.0) for x in leg_diags), default=0.0)
        diagnostics.append(
            {
                "bundle_id": bundle_id,
                "legs": leg_diags,
                "bundle_min_prior_capacity_ratio": min_capacity,
                "bundle_min_realized_clearance_ratio": min_realized,
                "recent_flow_could_clear_every_leg_within_execution_window": bool(leg_diags) and min_capacity >= 1.0,
                "observed_post_entry_flow_cleared_every_leg_queue": bool(leg_diags) and min_realized >= 1.0,
            }
        )

    return {
        "schema": "v6_graph_queue_feasibility_v1",
        "paper_only": True,
        "lookback_seconds": lookback_seconds,
        "execution_window_seconds": execution_window_seconds,
        "bundle_count": len(diagnostics),
        "bundles": diagnostics,
        "interpretation": (
            "Descriptive causal feasibility only. Prior-flow capacity is not a fill probability and must not be used as alpha. "
            "It is a fail-closed diagnostic for queue burdens that recent receive-time-observed compatible flow did not clear."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit receive-time causal queue feasibility for V6 Graph/RV paper bundles")
    parser.add_argument("--legs", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--execution-window-seconds", type=int, default=180)
    args = parser.parse_args()

    report = audit(
        load_csv(args.legs),
        load_csv(args.trade_tape),
        lookback_seconds=args.lookback_seconds,
        execution_window_seconds=args.execution_window_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"bundle_count": report["bundle_count"], "paper_only": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
