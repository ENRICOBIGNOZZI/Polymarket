#!/usr/bin/env python3
from __future__ import annotations

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


def finite(x, default=math.nan):
    try:
        y = float(x)
    except (TypeError, ValueError, OverflowError):
        return default
    return y if math.isfinite(y) else default


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def load(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as h:
        return [dict(r) for r in csv.DictReader(h)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--min-edge", type=float, default=0.0002)
    ap.add_argument("--stress-bps", type=float, default=10.0)
    ap.add_argument("--max-age-seconds", type=int, default=240)
    args = ap.parse_args()

    now = int(time.time())
    accepted: list[dict[str, str]] = []
    reject = Counter()
    relabeled = 0

    for row in load(args.input):
        missing = [k for k in FIELDS if k not in row]
        if missing:
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
            reject["identity"] += 1
            continue
        if created <= 0 or now - created > args.max_age_seconds or deadline <= now:
            reject["stale"] += 1
            continue
        if not math.isfinite(edge) or edge <= 0.0:
            reject["nonpositive_edge"] += 1
            continue
        if not math.isfinite(notional) or notional <= 0.0:
            reject["notional"] += 1
            continue
        if not math.isfinite(price) or not 0.0 < price < 1.0:
            reject["price"] += 1
            continue
        if not math.isfinite(weight) or weight <= 0.0:
            reject["weight"] += 1
            continue
        if side not in {"YES", "NO"}:
            reject["side"] += 1
            continue
        if mode not in {"MAKER", "TAKER"}:
            reject["mode"] += 1
            continue

        # Release gate: the frozen V6 structural threshold parser can group
        # contracts with different expiries and thereby overstate the guaranteed
        # payoff floor. Until the typed/date-aware relation semantics are validated,
        # fail closed rather than book potentially fictitious structural PnL.
        if strategy == "STRUCTURAL":
            reject["structural_payoff_unverified"] += 1
            continue

        # A resting multi-leg order is relative value, not hard arbitrage: legging
        # and partial-fill risk invalidate a deterministic payoff guarantee.
        if strategy == "GRAPH_HARD" and mode != "TAKER":
            row["strategy"] = "GRAPH_RV"
            strategy = "GRAPH_RV"
            relabeled += 1

        # Hard-arbitrage intents must be immediately executable. This guard keeps
        # future scanners from silently reintroducing maker semantics under the
        # GRAPH_HARD name.
        if strategy == "GRAPH_HARD" and mode != "TAKER":
            reject["hard_not_taker"] += 1
            continue

        # Statistical/graph maker edges need a positive buffer after a
        # deterministic extra cost/adverse-selection stress.
        stressed_edge = edge - max(0.0, args.stress_bps) / 10000.0
        if mode == "MAKER" and stressed_edge <= args.min_edge:
            reject["stress_edge"] += 1
            continue
        if mode == "TAKER" and edge <= args.min_edge:
            reject["edge"] += 1
            continue

        accepted.append(row)

    atomic_csv(args.output, accepted)
    status = {
        "timestamp": now,
        "paper_only": True,
        "input_rows": sum(reject.values()) + len(accepted),
        "accepted_rows": len(accepted),
        "relabeled_graph_hard_to_rv": relabeled,
        "structural_enabled": False,
        "rejections": dict(sorted(reject.items())),
        "strategies": dict(sorted(Counter(str(r["strategy"]) for r in accepted).items())),
        "best_edge": max((finite(r.get("expected_edge"), 0.0) for r in accepted), default=0.0),
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.status.with_name(
        args.status.name + f".tmp.{os.getpid()}.{threading.get_ident()}"
    )
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
