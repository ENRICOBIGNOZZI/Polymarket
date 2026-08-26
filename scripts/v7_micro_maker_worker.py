#!/usr/bin/env python3
from __future__ import annotations

import json as std_json
import sys
import time
from pathlib import Path
from typing import Any

import v7_maker_cancel_latency as cancel
import v7_micro_maker_worker_depth_core as depth

# The canonical maker is a cancel-latency wrapper around the frozen depth-aware
# core. Keep the safety-critical depth/markout contract explicit at this public
# adapter boundary rather than relying only on dynamic symbol re-export.
MAX_MARKOUT_LABEL_DELAY_SECONDS = 15
MARKOUT_REJECTION_REASON = "late_markout_label"
MARKOUT_LABEL_CONTRACT = "event_time_horizon_with_bounded_observation_delay"
EXIT_LIQUIDITY_CONTRACT = "shares_specific_full_visible_bid_depth_vwap_fail_closed"
full_depth_sell_vwap = depth.full_depth_sell_vwap
if depth.MAX_MARKOUT_LABEL_DELAY_SECONDS != MAX_MARKOUT_LABEL_DELAY_SECONDS:
    raise RuntimeError("maker depth-core markout delay contract drift")

for _name in dir(depth):
    if not _name.startswith("__") and _name != "main":
        globals()[_name] = getattr(depth, _name)


def _run_dir(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--run-dir")
        return Path(argv[index + 1])
    except (ValueError, IndexError):
        return None


def _consume_int_option(argv: list[str], name: str, default: int) -> int:
    if name not in argv:
        return int(default)
    index = argv.index(name)
    try:
        value = int(argv[index + 1])
    except (IndexError, ValueError):
        raise SystemExit(f"invalid {name}")
    del argv[index:index + 2]
    return value


class _JsonProxy:
    def __init__(self, module: Any, loads_fn: Any) -> None:
        self._module = module
        self.loads = loads_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


def main() -> int:
    argv = list(sys.argv)
    latency_ms = max(0, _consume_int_option(argv, "--cancel-latency-ms", cancel.DEFAULT_CANCEL_LATENCY_MS))
    grace_ms = max(0, _consume_int_option(argv, "--cancel-tape-grace-ms", cancel.DEFAULT_TAPE_GRACE_MS))
    run_dir = _run_dir(argv)
    processing_ms = time.time_ns() // 1_000_000

    event = depth.core
    original_argv = list(sys.argv)
    original_json = event.json
    original_append = event.base.append_csv
    original_eligible = event.causal_fill_eligible
    cancel_requests: dict[str, str] = {}

    def on_cancel(market_id: str, order: dict[str, Any]) -> bool:
        reason = cancel_requests.pop(market_id, "")
        if not reason:
            return False
        cancel.request_cancel(
            order,
            processing_ms=processing_ms,
            latency_ms=latency_ms,
            grace_ms=grace_ms,
            reason=reason,
        )
        return True

    def patched_loads(text: str, *args: Any, **kwargs: Any) -> Any:
        value = std_json.loads(text, *args, **kwargs)
        if isinstance(value, dict) and isinstance(value.get("orders"), dict):
            value["orders"] = cancel.CancelAwareOrders(value["orders"], on_cancel=on_cancel)
        return value

    def patched_append(path: Path, fields: list[str], row: dict[str, Any]) -> None:
        action = str(row.get("action") or "")
        if Path(path).name == "maker_order_log.csv" and action in {"CANCEL_TTL", "CANCEL_ZERO_CAUSAL_FLOW"}:
            market_id = str(row.get("market_id") or "")
            if market_id:
                cancel_requests[market_id] = action
            if str(row.get("order_state") or "OPEN") == "CANCEL_PENDING":
                return
            rewritten = dict(row)
            rewritten["action"] = "CANCEL_REQUEST_TTL" if action == "CANCEL_TTL" else "CANCEL_REQUEST_ZERO_CAUSAL_FLOW"
            original_append(path, fields, rewritten)
            return
        original_append(path, fields, row)

    def patched_eligible(row: dict[str, str], order: dict[str, Any], *, processing_ms: int, ttl_seconds: int) -> bool:
        return cancel.causal_fill_eligible(row, order, processing_ms=processing_ms, ttl_seconds=ttl_seconds)

    sys.argv = argv
    event.json = _JsonProxy(original_json, patched_loads)
    event.base.append_csv = patched_append
    event.causal_fill_eligible = patched_eligible
    try:
        # Replay every currently known receive-causal row first. A cancel-pending
        # order must still be present here so a delayed trade whose event time was
        # before the effective cancel cannot be silently dropped.
        rc = depth.main()
    finally:
        sys.argv = original_argv
        event.json = original_json
        event.base.append_csv = original_append
        event.causal_fill_eligible = original_eligible

    if run_dir is not None:
        after_replay_ms = time.time_ns() // 1_000_000
        finalized = cancel.finalize_due_cancels(run_dir, processing_ms=after_replay_ms)
        cancel.append_final_cancel_log(event.base, run_dir, finalized, timestamp=int(after_replay_ms // 1000))
        cancel.annotate_contract(run_dir, latency_ms=latency_ms, grace_ms=grace_ms)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
