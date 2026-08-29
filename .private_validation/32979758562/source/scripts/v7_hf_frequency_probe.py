#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def compatible_volume(rows: list[dict[str, str]], token: str, limit_price: float, start: int, end: int) -> float:
    total = 0.0
    for row in rows:
        ts = int(finite(row.get("timestamp"), 0.0))
        if not start <= ts < end:
            continue
        if str(row.get("asset_id") or row.get("token_id") or "") != token:
            continue
        if str(row.get("side") or "").upper() != "SELL":
            continue
        price = finite(row.get("price"), 2.0)
        size = max(0.0, finite(row.get("size"), 0.0))
        if price <= limit_price + 1e-12:
            total += size
    return total


def cadence_report(rows: list[dict[str, str]], orders: list[dict[str, str]], cadence: int, start: int, end: int) -> dict[str, Any]:
    buckets = max(1, math.ceil((end - start) / cadence))
    counts = [0] * buckets
    tokens: set[str] = set()
    for row in rows:
        ts = int(finite(row.get("timestamp"), 0.0))
        if not start <= ts < end:
            continue
        index = min(buckets - 1, max(0, (ts - start) // cadence))
        counts[index] += 1
        token = str(row.get("asset_id") or row.get("token_id") or "")
        if token:
            tokens.add(token)
    nonempty = [count for count in counts if count > 0]

    clearable = 0
    evaluated = 0
    clearance_ratios: list[float] = []
    for order in orders:
        token = str(order.get("token_id") or order.get("asset_id") or "")
        limit_price = finite(order.get("limit_price"), math.nan)
        queue = max(0.0, finite(order.get("queue_ahead"), 0.0))
        own = max(0.0, finite(order.get("remaining_shares"), finite(order.get("target_shares"), 0.0)))
        if not token or not math.isfinite(limit_price) or own <= 0.0:
            continue
        evaluated += 1
        burden = max(1e-12, queue + own)
        best = 0.0
        cursor = start
        while cursor < end:
            volume = compatible_volume(rows, token, limit_price, cursor, min(end, cursor + cadence))
            best = max(best, volume / burden)
            cursor += cadence
        clearance_ratios.append(best)
        if best >= 1.0:
            clearable += 1

    return {
        "cadence_seconds": cadence,
        "bucket_count": buckets,
        "nonempty_bucket_fraction": len(nonempty) / buckets,
        "mean_trades_per_bucket": sum(counts) / buckets,
        "median_trades_per_nonempty_bucket": statistics.median(nonempty) if nonempty else 0.0,
        "active_tokens": len(tokens),
        "maker_orders_evaluated": evaluated,
        "maker_orders_clearable_in_one_bucket": clearable,
        "maker_clearable_fraction": clearable / evaluated if evaluated else 0.0,
        "median_best_queue_clearance_ratio": statistics.median(clearance_ratios) if clearance_ratios else 0.0,
        "max_best_queue_clearance_ratio": max(clearance_ratios, default=0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare HF decision cadences on one causal public tape")
    parser.add_argument("--frequency-config", type=Path, default=Path("config/v7_frequency_matrix.json"))
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--maker-orders", type=Path, required=True)
    parser.add_argument("--lookback-seconds", type=int, default=300)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    cfg = json.loads(args.frequency_config.read_text(encoding="utf-8"))
    cadences = sorted({
        int(x)
        for values in cfg.get("execution_cadences_seconds", {}).values()
        for x in values
        if int(x) > 0
    })
    now = int(time.time())
    start = now - max(30, int(args.lookback_seconds))
    tape = read_csv(args.trade_tape)
    orders = read_csv(args.maker_orders)
    report = {
        "schema": "v7_hf_frequency_probe_v1",
        "timestamp": now,
        "paper_only": True,
        "authenticated_execution": False,
        "same_tape_comparison": True,
        "lookback_seconds": now - start,
        "trade_rows": sum(start <= int(finite(row.get("timestamp"), 0.0)) < now for row in tape),
        "maker_order_rows": len(orders),
        "cadences": [cadence_report(tape, orders, cadence, start, now) for cadence in cadences],
        "interpretation": "Execution cadence diagnostic only; cadence selection requires fill-conditioned PnL/markout, not activity density alone."
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
