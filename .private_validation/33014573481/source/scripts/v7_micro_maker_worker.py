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
REPLAY_CONTINUITY_CONTRACT = "tracked_market_and_token_book_required_before_tape_replay"
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


def _load_state(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    try:
        value = std_json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, std_json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def tracked_market_token_pairs(state: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for key in ("orders", "positions", "markout_watch"):
        rows = state.get(key) if isinstance(state.get(key), dict) else {}
        for row in rows.values():
            if not isinstance(row, dict):
                continue
            market_id = str(row.get("market_id") or "")
            token_id = str(row.get("token_id") or "")
            if market_id and token_id:
                pairs.add((market_id, token_id))
    return pairs


def replay_continuity_gaps(
    state: dict[str, Any],
    *,
    discovered_market_ids: set[str],
    book_tokens: set[str],
) -> dict[str, list[str]]:
    tracked = tracked_market_token_pairs(state)
    missing_markets = sorted({market_id for market_id, _ in tracked if market_id not in discovered_market_ids})
    missing_books = sorted({token_id for _, token_id in tracked if token_id not in book_tokens})
    return {"missing_market_ids": missing_markets, "missing_book_tokens": missing_books}


def _write_continuity_audit(run_dir: Path, value: dict[str, Any]) -> None:
    path = run_dir / "maker_replay_continuity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{time.time_ns()}")
    tmp.write_text(std_json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


class ReplayContinuityError(RuntimeError):
    pass


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
    tracked_state = _load_state(run_dir)

    event = depth.core
    original_argv = list(sys.argv)
    original_json = event.json
    original_append = event.base.append_csv
    original_eligible = event.causal_fill_eligible
    original_discover = event.base.discover
    original_fetch_books = event.base.fetch_books
    original_load_tape = event.load_tape
    cancel_requests: dict[str, str] = {}
    discovered_market_ids: set[str] = set()
    book_tokens: set[str] = set()
    continuity_block: dict[str, Any] | None = None

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

    def patched_discover(*args: Any, **kwargs: Any) -> Any:
        markets = original_discover(*args, **kwargs)
        discovered_market_ids.clear()
        discovered_market_ids.update(str(getattr(market, "id", "")) for market in markets if getattr(market, "id", ""))
        return markets

    def patched_fetch_books(*args: Any, **kwargs: Any) -> Any:
        books = original_fetch_books(*args, **kwargs)
        book_tokens.clear()
        if isinstance(books, dict):
            book_tokens.update(str(token) for token in books)
        return books

    def patched_load_tape(path: Path, cutoff: int, now: int) -> list[dict[str, str]]:
        gaps = replay_continuity_gaps(
            tracked_state,
            discovered_market_ids=discovered_market_ids,
            book_tokens=book_tokens,
        )
        if gaps["missing_market_ids"] or gaps["missing_book_tokens"]:
            raise ReplayContinuityError(std_json.dumps(gaps, sort_keys=True))
        return original_load_tape(path, cutoff, now)

    sys.argv = argv
    event.json = _JsonProxy(original_json, patched_loads)
    event.base.append_csv = patched_append
    event.causal_fill_eligible = patched_eligible
    event.base.discover = patched_discover
    event.base.fetch_books = patched_fetch_books
    event.load_tape = patched_load_tape
    rc = 0
    try:
        # Replay every currently known receive-causal row first. A cancel-pending
        # order must still be present here so a delayed trade whose event time was
        # before the effective cancel cannot be silently dropped.
        rc = depth.main()
    except ReplayContinuityError as exc:
        continuity_block = {
            "timestamp_ms": processing_ms,
            "paper_only": True,
            "authenticated_execution": False,
            "contract": REPLAY_CONTINUITY_CONTRACT,
            "action": "FAIL_CLOSED_NO_REPLAY_NO_CANCEL",
            "details": std_json.loads(str(exc)),
            "tracked_pairs": sorted([list(pair) for pair in tracked_market_token_pairs(tracked_state)]),
        }
        rc = 2
    finally:
        sys.argv = original_argv
        event.json = original_json
        event.base.append_csv = original_append
        event.causal_fill_eligible = original_eligible
        event.base.discover = original_discover
        event.base.fetch_books = original_fetch_books
        event.load_tape = original_load_tape

    if continuity_block is not None:
        if run_dir is not None:
            _write_continuity_audit(run_dir, continuity_block)
        print(std_json.dumps(continuity_block, sort_keys=True))
        return rc

    if run_dir is not None:
        after_replay_ms = time.time_ns() // 1_000_000
        finalized = cancel.finalize_due_cancels(run_dir, processing_ms=after_replay_ms)
        cancel.append_final_cancel_log(event.base, run_dir, finalized, timestamp=int(after_replay_ms // 1000))
        cancel.annotate_contract(run_dir, latency_ms=latency_ms, grace_ms=grace_ms)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
