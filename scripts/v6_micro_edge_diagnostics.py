#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from v6_micro_taker import discover, fee_per_share, fetch_books, features, resolve_fee_details


def finite(value: Any, default: float = math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def quantile(values: list[float], q: float) -> float | None:
    xs = sorted(x for x in values if math.isfinite(x))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = max(0.0, min(1.0, q)) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def stats(values: list[float]) -> dict[str, float | None]:
    xs = [x for x in values if math.isfinite(x)]
    return {
        "min": min(xs) if xs else None,
        "p50": quantile(xs, 0.50),
        "p90": quantile(xs, 0.90),
        "p99": quantile(xs, 0.99),
        "max": max(xs) if xs else None,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Decompose V6 micro forecast edge into crossing, slippage and fee costs.")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--markets", type=int, default=500)
    ap.add_argument("--min-liquidity", type=float, default=25.0)
    ap.add_argument("--min-edge", type=float, default=0.00030)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    now = int(time.time())
    status: dict[str, Any] = {
        "timestamp": now,
        "paper_only": True,
        "min_edge": args.min_edge,
        "slippage_bps": args.slippage_bps,
        "failures": [],
    }
    state_path = args.run_dir / "state.json"
    if not state_path.exists():
        status.update({"status": "WARMUP", "reason": "missing_state"})
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        status.update({"status": "WARMUP", "reason": f"unreadable_state:{type(exc).__name__}:{exc}"})
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    beta = state.get("beta") if isinstance(state.get("beta"), list) else []
    labeled = int(finite(state.get("labeled_samples"), 0.0))
    if len(beta) != 6 or labeled < 40:
        status.update({"status": "WARMUP", "reason": "insufficient_labeled_samples", "labeled_samples": labeled})
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    try:
        markets = discover(gamma, args.markets, args.min_liquidity)
        fee_stats = resolve_fee_details(clob, markets)
        books = fetch_books(clob, markets)
    except Exception as exc:
        status["failures"].append(f"market_data:{type(exc).__name__}:{exc}")
        status.update({"status": "FAILED", "labeled_samples": labeled})
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    slip = max(0.0, args.slippage_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    for market in markets:
        yes, no = books.get(market.yes), books.get(market.no)
        if not yes or not no:
            continue
        z = features(yes, no)
        if not z:
            continue
        x, yes_mid, spread = z
        pred = sum(float(a) * float(b) for a, b in zip(beta, x))
        pred = max(-2 * spread, min(2 * spread, pred))
        fair_yes = max(0.001, min(0.999, yes_mid + pred))
        for side, book, fair, side_mid in (
            ("YES", yes, fair_yes, yes_mid),
            ("NO", no, 1.0 - fair_yes, 1.0 - yes_mid),
        ):
            ask = book.ask()
            if not math.isfinite(ask):
                continue
            entry = min(0.999999, ask * (1.0 + slip))
            fee = fee_per_share(entry, market.fee_rate, market.fee_exp)
            forecast_vs_mid = fair - side_mid
            cross_slip_cost = entry - side_mid
            pre_fee_edge = fair - entry
            net_edge = pre_fee_edge - fee
            rows.append(
                {
                    "market_id": market.id,
                    "slug": market.slug,
                    "side": side,
                    "mid": side_mid,
                    "ask": ask,
                    "entry": entry,
                    "fair": fair,
                    "spread": spread,
                    "forecast_vs_mid": forecast_vs_mid,
                    "cross_slip_cost": cross_slip_cost,
                    "fee_per_share": fee,
                    "pre_fee_edge": pre_fee_edge,
                    "net_edge": net_edge,
                    "fee_rate": market.fee_rate,
                    "fee_source": market.fee_source,
                }
            )

    net = [float(r["net_edge"]) for r in rows]
    pre = [float(r["pre_fee_edge"]) for r in rows]
    forecasts = [float(r["forecast_vs_mid"]) for r in rows]
    crossing = [float(r["cross_slip_cost"]) for r in rows]
    fees = [float(r["fee_per_share"]) for r in rows]
    rows.sort(key=lambda r: float(r["net_edge"]), reverse=True)
    top_rows = rows[: max(1, args.top)]

    status.update(
        {
            "status": "READY",
            "markets_requested": args.markets,
            "markets_discovered": len(markets),
            "candidate_sides_evaluated": len(rows),
            "labeled_samples": labeled,
            "fee_stats": fee_stats,
            "counts": {
                "forecast_positive_vs_mid": sum(x > 0.0 for x in forecasts),
                "pre_fee_edge_positive": sum(x > 0.0 for x in pre),
                "net_edge_positive": sum(x > 0.0 for x in net),
                "above_min_edge": sum(x > args.min_edge for x in net),
            },
            "forecast_vs_mid": stats(forecasts),
            "cross_plus_slippage_cost": stats(crossing),
            "fee_per_share": stats(fees),
            "pre_fee_edge": stats(pre),
            "net_edge": stats(net),
            "top": top_rows,
        }
    )
    atomic_json(args.output, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
