#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def decayed_rate(trades: list[dict[str, str]], *, decision_ts: int, lookback_seconds: int, clock: str) -> float:
    window = max(1.0, float(lookback_seconds))
    half_life = max(5.0, window / 3.0)
    decay = math.log(2.0) / half_life
    weighted = 0.0
    for trade in trades:
        if clock == "event":
            observed_ts = finite(trade.get("timestamp"), 0.0)
        elif clock == "receive":
            observed_ts = finite(trade.get("received_ms"), 0.0) / 1000.0
        else:
            raise ValueError(f"unknown clock: {clock}")
        age = max(0.0, decision_ts - observed_ts)
        weighted += max(0.0, finite(trade.get("size"), 0.0)) * math.exp(-decay * age)
    effective_seconds = (1.0 - math.exp(-decay * window)) / decay
    return weighted / max(effective_seconds, 1e-9)


def audit(order_log: Path, trade_tape: Path, *, lookback_seconds: int = 120) -> dict[str, Any]:
    orders = [row for row in rows(order_log) if str(row.get("action") or "").upper() == "POST"]
    tape = rows(trade_tape)
    output: list[dict[str, Any]] = []
    for order in orders:
        decision_ts = int(finite(order.get("timestamp"), 0.0))
        token = str(order.get("token_id") or "")
        limit_price = finite(order.get("limit_price"), 0.0)
        prior: list[dict[str, str]] = []
        for trade in tape:
            event_ts = int(finite(trade.get("timestamp"), 0.0))
            received_ms = int(finite(trade.get("received_ms"), 0.0))
            if event_ts < decision_ts - lookback_seconds or event_ts > decision_ts + 30:
                continue
            if received_ms < (decision_ts - lookback_seconds) * 1000 or received_ms > decision_ts * 1000:
                continue
            if str(trade.get("asset_id") or "") != token:
                continue
            if str(trade.get("side") or "").upper() != "SELL":
                continue
            if finite(trade.get("price"), 2.0) > limit_price + 1e-12:
                continue
            prior.append(trade)
        newest_event = max((finite(row.get("timestamp"), 0.0) for row in prior), default=0.0)
        newest_receive = max((finite(row.get("received_ms"), 0.0) / 1000.0 for row in prior), default=0.0)
        output.append({
            "market_id": str(order.get("market_id") or ""),
            "side": str(order.get("side") or ""),
            "token_id": token,
            "decision_ts": decision_ts,
            "limit_price": limit_price,
            "prior_compatible_sell_trades": len(prior),
            "prior_compatible_sell_shares": sum(max(0.0, finite(row.get("size"), 0.0)) for row in prior),
            "newest_event_age_seconds": decision_ts - newest_event if newest_event else None,
            "newest_receive_age_seconds": decision_ts - newest_receive if newest_receive else None,
            "receive_age_rate_per_second": decayed_rate(prior, decision_ts=decision_ts, lookback_seconds=lookback_seconds, clock="receive"),
            "event_age_rate_per_second": decayed_rate(prior, decision_ts=decision_ts, lookback_seconds=lookback_seconds, clock="event"),
        })
    delayed = [row for row in output if row["newest_event_age_seconds"] is not None and row["newest_receive_age_seconds"] is not None and row["newest_event_age_seconds"] - row["newest_receive_age_seconds"] >= 10]
    return {
        "schema_version": 1,
        "paper_only": True,
        "authenticated_execution": False,
        "posts": len(output),
        "posts_with_10s_plus_event_receive_age_gap": len(delayed),
        "rows": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit maker fill hazard for delayed public trade delivery")
    parser.add_argument("--order-log", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--lookback-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.order_log, args.trade_tape, lookback_seconds=max(1, args.lookback_seconds))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
