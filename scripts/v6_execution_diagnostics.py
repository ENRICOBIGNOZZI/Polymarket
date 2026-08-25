#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = max(0.0, min(1.0, q)) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def summarize_ratios(rows: list[dict[str, str]], queue_key: str, remaining_fn) -> dict[str, Any]:
    ratios: list[float] = []
    deep = 0
    severe = 0
    for row in rows:
        remaining = max(0.0, remaining_fn(row))
        queue = max(0.0, finite(row.get(queue_key)))
        if remaining <= 1e-9:
            continue
        ratio = queue / remaining
        ratios.append(ratio)
        if ratio > 50.0:
            deep += 1
        if ratio > 200.0:
            severe += 1
    return {
        "observations": len(ratios),
        "queue_to_remaining_p50": quantile(ratios, 0.50),
        "queue_to_remaining_p90": quantile(ratios, 0.90),
        "queue_to_remaining_max": max(ratios, default=0.0),
        "deep_queue_gt_50": deep,
        "severe_queue_gt_200": severe,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize V6 paper execution pressure and fill progress.")
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    root = args.run_root

    maker_orders = read_csv(root / "maker" / "maker_orders.csv")
    maker_fills = read_csv(root / "maker" / "maker_fills.csv")
    multileg = read_csv(root / "multileg_legs.csv")
    bundle_ledger = read_csv(root / "bundle_ledger.csv")

    maker_summary = summarize_ratios(
        maker_orders,
        "queue_ahead",
        lambda r: finite(r.get("remaining_shares")),
    )
    multileg_summary = summarize_ratios(
        [r for r in multileg if r.get("order_state") in {"RESTING", "CANCEL_PENDING"}],
        "queue_ahead",
        lambda r: max(0.0, finite(r.get("target_shares")) - finite(r.get("filled_shares"))),
    )

    maker_buys = sum(1 for r in maker_fills if str(r.get("action", "")).startswith("BUY_MAKER"))
    maker_sells = sum(1 for r in maker_fills if str(r.get("action", "")).startswith("SELL_TAKER"))
    completed_bundles = sum(1 for r in bundle_ledger if r.get("status") == "CLOSED")
    unwound_bundles = sum(1 for r in bundle_ledger if r.get("status") == "UNWOUND")
    multileg_partial = sum(1 for r in multileg if finite(r.get("filled_shares")) > 1e-9)

    queue_statuses: dict[str, Any] = {}
    for name in ("relation_queue_status.json", "local_factor_queue_status.json"):
        path = root / name
        if path.exists() and path.stat().st_size:
            try:
                queue_statuses[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                queue_statuses[name] = {"error": "unreadable"}

    diagnosis = "HEALTHY_EXECUTION"
    if maker_summary["severe_queue_gt_200"] or multileg_summary["severe_queue_gt_200"]:
        diagnosis = "SEVERE_PASSIVE_QUEUE"
    elif maker_summary["deep_queue_gt_50"] or multileg_summary["deep_queue_gt_50"]:
        diagnosis = "PASSIVE_QUEUE_PRESSURE"
    elif maker_buys == 0 and completed_bundles == 0 and (maker_orders or multileg):
        diagnosis = "RESTING_WITHOUT_FILLS"

    status = {
        "timestamp": int(time.time()),
        "paper_only": True,
        "diagnosis": diagnosis,
        "maker": {
            **maker_summary,
            "resting_orders": len(maker_orders),
            "maker_buy_fill_events": maker_buys,
            "maker_exit_events": maker_sells,
        },
        "multileg": {
            **multileg_summary,
            "legs_with_any_fill": multileg_partial,
            "closed_bundles": completed_bundles,
            "unwound_bundles": unwound_bundles,
        },
        "queue_filters": queue_statuses,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.output)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
