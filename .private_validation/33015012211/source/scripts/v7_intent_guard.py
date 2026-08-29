#!/usr/bin/env python3
from __future__ import annotations

# V7 bundle admission guard. Structural relations remain fail-closed until their
# typed expiry/payoff semantics are independently validated; maker Graph baskets
# are explicitly relative value rather than hard arbitrage.

import argparse
import csv
import json
import math
import os
import threading
import time
from collections import Counter
from pathlib import Path

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


def finite(value, default=math.nan):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def load(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 static bundle intent guard")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--stress-bps", type=float, default=10.0)
    parser.add_argument("--max-age-seconds", type=int, default=240)
    args = parser.parse_args()

    now = int(time.time())
    accepted: list[dict[str, str]] = []
    reject = Counter()
    relabeled = 0

    for row in load(args.input):
        if any(key not in row for key in FIELDS):
            reject["schema"] += 1
            continue
        strategy = str(row.get("strategy") or "").strip().upper()
        mode = str(row.get("mode") or "").strip().upper()
        created = int(finite(row.get("created_ts"), 0.0))
        deadline = int(finite(row.get("execution_deadline_ts"), 0.0))
        edge = finite(row.get("expected_edge"))
        notional = finite(row.get("max_notional"))
        price = finite(row.get("limit_price"))
        weight = finite(row.get("weight"))
        side = str(row.get("side") or "").strip().upper()
        if not strategy or not row.get("bundle_id") or not row.get("market_id"):
            reject["identity"] += 1; continue
        if created <= 0 or now - created > args.max_age_seconds or deadline <= now:
            reject["stale"] += 1; continue
        if not math.isfinite(edge) or edge <= 0.0:
            reject["nonpositive_edge"] += 1; continue
        if not math.isfinite(notional) or notional <= 0.0:
            reject["notional"] += 1; continue
        if not math.isfinite(price) or not 0.0 < price < 1.0:
            reject["price"] += 1; continue
        if not math.isfinite(weight) or weight <= 0.0:
            reject["weight"] += 1; continue
        if side not in {"YES", "NO"}:
            reject["side"] += 1; continue
        if mode not in {"MAKER", "TAKER"}:
            reject["mode"] += 1; continue

        if strategy == "STRUCTURAL":
            reject["structural_payoff_unverified"] += 1
            continue
        if strategy == "GRAPH_HARD" and mode != "TAKER":
            row["strategy"] = "GRAPH_RV"
            strategy = "GRAPH_RV"
            relabeled += 1
        if strategy == "GRAPH_HARD" and mode != "TAKER":
            reject["hard_not_taker"] += 1
            continue

        stressed_edge = edge - max(0.0, args.stress_bps) / 10000.0
        if mode == "MAKER" and stressed_edge <= args.min_edge:
            reject["stress_edge"] += 1; continue
        if mode == "TAKER" and edge <= args.min_edge:
            reject["edge"] += 1; continue
        accepted.append(row)

    atomic_csv(args.output, accepted)
    status = {
        "schema": "polymarket_v7_intent_guard_status_v1",
        "timestamp": now,
        "paper_only": True,
        "input_rows": sum(reject.values()) + len(accepted),
        "accepted_rows": len(accepted),
        "relabeled_graph_hard_to_rv": relabeled,
        "structural_enabled": False,
        "rejections": dict(sorted(reject.items())),
        "strategies": dict(sorted(Counter(str(row["strategy"]) for row in accepted).items())),
        "best_edge": max((finite(row.get("expected_edge"), 0.0) for row in accepted), default=0.0),
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.status.with_name(args.status.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
