#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import v6_micro_maker as base
except ModuleNotFoundError:
    from scripts import v6_micro_maker as base


def _arg_value(flag: str, default: str = "") -> str:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return default
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else default


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def _recent_compatible_flow(
    tape: Path,
    *,
    token: str,
    limit_price: float,
    created_ts: int,
    now: int,
) -> float:
    if not tape.exists() or tape.stat().st_size == 0:
        return 0.0
    total = 0.0
    try:
        with tape.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ts = int(_finite(row.get("timestamp"), 0.0))
                if ts <= created_ts or ts > now + 30:
                    continue
                if str(row.get("asset_id") or "") != token:
                    continue
                if str(row.get("side") or "").upper() != "SELL":
                    continue
                price = _finite(row.get("price"), 2.0)
                size = max(0.0, _finite(row.get("size"), 0.0))
                if price <= limit_price + 1e-12:
                    total += size
    except OSError:
        return 0.0
    return total


def recycle_dead_orders(run_dir: Path, trade_tape: Path, *, grace_seconds: int) -> dict[str, int]:
    """Cancel resting paper maker orders that have observed no causal contra-flow.

    The V6 maker already gates *new* orders on observed compatible flow. This
    pre-pass prevents capital from remaining trapped when flow disappears after
    entry. It does not create fills, cross the spread, or convert maker orders to
    taker orders.
    """
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return {"examined": 0, "cancelled": 0}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"examined": 0, "cancelled": 0}
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    if not orders:
        return {"examined": 0, "cancelled": 0}

    now = int(time.time())
    cancelled = 0
    examined = 0
    for market_id, order in list(orders.items()):
        examined += 1
        created = int(_finite(order.get("created_ts"), now))
        if now - created < max(1, grace_seconds):
            continue
        flow = _recent_compatible_flow(
            trade_tape,
            token=str(order.get("token_id") or ""),
            limit_price=_finite(order.get("limit_price"), 0.0),
            created_ts=created,
            now=now,
        )
        if flow > 1e-12:
            continue
        if hasattr(base, "append_csv"):
            fields = [
                "timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price",
                "remaining_shares", "queue_ahead", "signal_edge", "confidence", "fill_probability",
                "expected_fill_edge", "flow_rate", "fee_source",
            ]
            base.append_csv(
                run_dir / "maker_order_log.csv",
                fields,
                {**order, "timestamp": now, "action": "CANCEL_ZERO_CAUSAL_FLOW"},
            )
        del orders[market_id]
        cancelled += 1

    if cancelled:
        state["orders"] = orders
        state["dead_flow_cancellations_total"] = int(_finite(state.get("dead_flow_cancellations_total"), 0.0)) + cancelled
        state["dead_flow_cancellations_last_tick"] = cancelled
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, state_path)
    return {"examined": examined, "cancelled": cancelled}


def main() -> int:
    run_dir_text = _arg_value("--run-dir")
    tape_text = _arg_value("--trade-tape")
    grace = int(os.environ.get("V6_MAKER_DEAD_FLOW_CANCEL_SECONDS", "20"))
    if run_dir_text and tape_text:
        stats = recycle_dead_orders(Path(run_dir_text), Path(tape_text), grace_seconds=grace)
        if stats["cancelled"]:
            print(json.dumps({"maker_dead_flow_recycle": stats}, sort_keys=True), file=sys.stderr)
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
