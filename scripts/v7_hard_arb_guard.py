#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import v7_hard_arb_core as q


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def consume_int(flag: str, default: int) -> int:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return max(0, int(default))
    raw = sys.argv[index + 1] if index + 1 < len(sys.argv) else str(default)
    del sys.argv[index : min(len(sys.argv), index + 2)]
    return max(0, int(finite(raw, float(default))))


def peek_float(flag: str, default: float) -> float:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return float(default)
    raw = sys.argv[index + 1] if index + 1 < len(sys.argv) else str(default)
    return finite(raw, float(default))


def executable_abort_mark(
    clob: str,
    aborting: dict[str, Any],
    *,
    slippage_bps: float,
    stats: dict[str, Any] | None = None,
) -> float:
    """Full-depth, post-fee executable liquidation mark for abort residuals.

    If a residual cannot be fully liquidated on visible bid depth or its fee
    schedule cannot be verified, its mark is zero (fail closed).
    """
    value = 0.0
    marked = 0
    unmarkable = 0
    for bundle in aborting.values():
        legs = bundle.get("legs", []) if isinstance(bundle, dict) else []
        for leg in legs if isinstance(legs, list) else []:
            if not isinstance(leg, dict):
                unmarkable += 1
                continue
            token = str(leg.get("token") or "")
            shares = max(0.0, finite(leg.get("shares"), 0.0))
            market = leg.get("market") if isinstance(leg.get("market"), dict) else {}
            if not token or shares <= 0.0 or not market:
                unmarkable += 1
                continue
            try:
                book = q.books(clob, [token]).get(token)
                if not book or not book.get("bids"):
                    raise RuntimeError("missing_bid_depth")
                details = q.resolve_fee_details(market, clob, q.get_json)
                fill = q.walk_book_for_shares(
                    book["bids"],
                    shares,
                    details,
                    buy=False,
                    slippage_bps=slippage_bps,
                    require_full=True,
                )
                if fill is None or not fill.complete:
                    raise RuntimeError("insufficient_full_depth")
                value += max(0.0, fill.stressed_cash - fill.fee)
                marked += 1
            except Exception:
                unmarkable += 1
    if stats is not None:
        stats["abort_mark_marked_legs"] = int(stats.get("abort_mark_marked_legs", 0)) + marked
        stats["abort_mark_unmarkable_legs"] = int(stats.get("abort_mark_unmarkable_legs", 0)) + unmarkable
    return value


def normalize_timestamp_ms(value: Any) -> int:
    ts = finite(value, 0.0)
    if ts <= 0.0:
        return 0
    if ts >= 1e14:
        ts /= 1000.0
    elif ts < 1e11:
        ts *= 1000.0
    return int(ts)


def local_book_freshness(
    live: dict[str, dict[str, Any]],
    tokens: list[str],
    *,
    now_ms: int,
    max_leg_age_ms: int,
    max_cross_leg_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps: list[int] = []
    for token in tokens:
        book = live.get(token)
        if not isinstance(book, dict):
            return False, "missing_book", 0, 0
        received_ms = int(finite(book.get("received_ms"), 0.0))
        if received_ms <= 0:
            return False, "missing_receive_timestamp", 0, 0
        stamps.append(received_ms)
    age = max(0, now_ms - min(stamps)) if stamps else 0
    skew = max(stamps) - min(stamps) if stamps else 0
    if age > max_leg_age_ms:
        return False, "max_leg_age", age, skew
    if skew > max_cross_leg_skew_ms:
        return False, "cross_leg_skew", age, skew
    return True, "ok", age, skew


def exchange_book_freshness(
    live: dict[str, dict[str, Any]],
    tokens: list[str],
    *,
    now_ms: int,
    max_snapshot_age_ms: int,
    max_snapshot_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps: list[int] = []
    for token in tokens:
        book = live.get(token)
        if not isinstance(book, dict):
            return False, "missing_book", 0, 0
        exchange_ms = int(finite(book.get("exchange_ts_ms"), 0.0))
        if exchange_ms <= 0:
            return False, "missing_exchange_timestamp", 0, 0
        stamps.append(exchange_ms)
    age = max(0, now_ms - min(stamps)) if stamps else 0
    skew = max(stamps) - min(stamps) if stamps else 0
    if age > max_snapshot_age_ms:
        return False, "max_exchange_snapshot_age", age, skew
    if skew > max_snapshot_skew_ms:
        return False, "exchange_snapshot_skew", age, skew
    return True, "ok", age, skew


def install_guard(
    *,
    max_leg_age_ms: int,
    max_cross_leg_skew_ms: int,
    max_exchange_snapshot_age_ms: int,
    max_exchange_snapshot_skew_ms: int,
    abort_slippage_bps: float,
) -> dict[str, Any]:
    original_plan = q._plan
    original_hard_fee = q._hard_fee
    stats: dict[str, Any] = {
        "book_calls": 0,
        "book_batches": 0,
        "freshness_checks": 0,
        "receive_rejections": 0,
        "exchange_rejections": 0,
        "unverified_fee_rejections": 0,
        "abort_mark_marked_legs": 0,
        "abort_mark_unmarkable_legs": 0,
        "max_observed_leg_age_ms": 0,
        "max_observed_cross_leg_skew_ms": 0,
        "max_observed_exchange_snapshot_age_ms": 0,
        "max_observed_exchange_snapshot_skew_ms": 0,
        "max_observed_batch_receive_span_ms": 0,
    }

    def timestamped_books(clob: str, tokens: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        receipts: list[int] = []
        for offset in range(0, len(tokens), 80):
            raw = q.get_json(
                clob.rstrip("/") + "/books",
                [{"token_id": token} for token in tokens[offset : offset + 80]],
            )
            received_ms = int(time.time() * 1000)
            receipts.append(received_ms)
            stats["book_batches"] = int(stats["book_batches"]) + 1
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                book = q._parse_book(item)
                if book is None:
                    continue
                book["received_ms"] = received_ms
                book["exchange_ts_ms"] = normalize_timestamp_ms(item.get("timestamp"))
                out[str(book["token"])] = book
        if receipts:
            stats["max_observed_batch_receive_span_ms"] = max(
                int(stats["max_observed_batch_receive_span_ms"]),
                max(receipts) - min(receipts),
            )
        stats["book_calls"] = int(stats["book_calls"]) + 1
        return out

    def guarded_plan(
        live: dict[str, dict[str, Any]],
        tokens: list[str],
        fees: dict[str, Any],
        shares: float,
        slip: float,
    ):
        now_ms = int(time.time() * 1000)
        receive_ok, _, age, skew = local_book_freshness(
            live,
            tokens,
            now_ms=now_ms,
            max_leg_age_ms=max_leg_age_ms,
            max_cross_leg_skew_ms=max_cross_leg_skew_ms,
        )
        stats["freshness_checks"] = int(stats["freshness_checks"]) + 1
        stats["max_observed_leg_age_ms"] = max(int(stats["max_observed_leg_age_ms"]), age)
        stats["max_observed_cross_leg_skew_ms"] = max(int(stats["max_observed_cross_leg_skew_ms"]), skew)
        if not receive_ok:
            stats["receive_rejections"] = int(stats["receive_rejections"]) + 1
            return None
        exchange_ok, _, exchange_age, exchange_skew = exchange_book_freshness(
            live,
            tokens,
            now_ms=now_ms,
            max_snapshot_age_ms=max_exchange_snapshot_age_ms,
            max_snapshot_skew_ms=max_exchange_snapshot_skew_ms,
        )
        stats["max_observed_exchange_snapshot_age_ms"] = max(
            int(stats["max_observed_exchange_snapshot_age_ms"]), exchange_age
        )
        stats["max_observed_exchange_snapshot_skew_ms"] = max(
            int(stats["max_observed_exchange_snapshot_skew_ms"]), exchange_skew
        )
        if not exchange_ok:
            stats["exchange_rejections"] = int(stats["exchange_rejections"]) + 1
            return None
        return original_plan(live, tokens, fees, shares, slip)

    def verified_hard_fee(raw: dict[str, Any], clob: str, cfg: dict[str, Any], sources: Any):
        details = original_hard_fee(raw, clob, cfg, sources)
        source = str(getattr(details, "source", ""))
        enabled = bool(getattr(details, "enabled", True))
        if not source or source.startswith("fallback:") or source == "unknown":
            stats["unverified_fee_rejections"] = int(stats["unverified_fee_rejections"]) + 1
            raise RuntimeError(f"unverified_fee_schedule:{source or 'missing'}")
        if enabled and finite(getattr(details, "rate", math.nan)) < 0:
            stats["unverified_fee_rejections"] = int(stats["unverified_fee_rejections"]) + 1
            raise RuntimeError("invalid_fee_schedule")
        return details

    q.books = timestamped_books
    q._plan = guarded_plan
    q._hard_fee = verified_hard_fee
    q._abort_mark = lambda clob, aborting: executable_abort_mark(
        clob,
        aborting,
        slippage_bps=abort_slippage_bps,
        stats=stats,
    )
    return stats


def annotate_status(
    run_dir: Path,
    stats: dict[str, Any],
    *,
    max_leg_age_ms: int,
    max_cross_leg_skew_ms: int,
    max_exchange_snapshot_age_ms: int,
    max_exchange_snapshot_skew_ms: int,
) -> None:
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    status.update(
        {
            "paper_only": True,
            "authenticated_execution": False,
            "atomic_snapshot_assumption": False,
            "per_token_receive_timestamps": True,
            "exchange_snapshot_timestamps": True,
            "max_leg_age_ms": max_leg_age_ms,
            "max_cross_leg_skew_ms": max_cross_leg_skew_ms,
            "max_exchange_snapshot_age_ms": max_exchange_snapshot_age_ms,
            "max_exchange_snapshot_skew_ms": max_exchange_snapshot_skew_ms,
            "multi_level_depth": True,
            "verified_fees_required": True,
            "sequential_leg_revalidation": True,
            "unwind_on_leg_failure": True,
            "abort_mark_mode": "full_depth_executable_liquidation",
            "abort_mark_fail_closed": True,
            "freshness_guard": stats,
        }
    )
    tmp = status_path.with_suffix(status_path.suffix + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(status_path)


def self_test() -> int:
    live = {
        "a": {"received_ms": 10_000, "exchange_ts_ms": 9_990},
        "b": {"received_ms": 10_040, "exchange_ts_ms": 10_010},
    }
    ok, reason, age, skew = local_book_freshness(
        live, ["a", "b"], now_ms=10_100, max_leg_age_ms=200, max_cross_leg_skew_ms=100
    )
    assert ok and reason == "ok" and age == 100 and skew == 40
    ok, reason, age, skew = exchange_book_freshness(
        live, ["a", "b"], now_ms=10_100, max_snapshot_age_ms=200, max_snapshot_skew_ms=100
    )
    assert ok and reason == "ok" and age == 110 and skew == 20
    ok, reason, _, _ = local_book_freshness(
        live, ["a", "b"], now_ms=10_500, max_leg_age_ms=200, max_cross_leg_skew_ms=100
    )
    assert not ok and reason == "max_leg_age"
    live["b"]["received_ms"] = 10_300
    ok, reason, _, _ = local_book_freshness(
        live, ["a", "b"], now_ms=10_320, max_leg_age_ms=500, max_cross_leg_skew_ms=100
    )
    assert not ok and reason == "cross_leg_skew"
    assert normalize_timestamp_ms(1_787_700_000) == 1_787_700_000_000
    assert normalize_timestamp_ms(1_787_700_000_123) == 1_787_700_000_123
    print("v7_hard_arb_guard_self_test=ok")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        return self_test()
    run_dir_text = ""
    try:
        index = sys.argv.index("--run-dir")
        run_dir_text = sys.argv[index + 1]
    except (ValueError, IndexError):
        pass
    abort_slippage_bps = max(0.0, peek_float("--slippage-bps", 5.0))
    max_leg_age_ms = consume_int("--max-leg-age-ms", 2000)
    max_cross_leg_skew_ms = consume_int("--max-cross-leg-skew-ms", 1000)
    max_exchange_snapshot_age_ms = consume_int("--max-exchange-snapshot-age-ms", 5000)
    max_exchange_snapshot_skew_ms = consume_int("--max-exchange-snapshot-skew-ms", 1000)
    stats = install_guard(
        max_leg_age_ms=max_leg_age_ms,
        max_cross_leg_skew_ms=max_cross_leg_skew_ms,
        max_exchange_snapshot_age_ms=max_exchange_snapshot_age_ms,
        max_exchange_snapshot_skew_ms=max_exchange_snapshot_skew_ms,
        abort_slippage_bps=abort_slippage_bps,
    )
    sys.argv = [sys.argv[0], "hard", *sys.argv[1:]]
    rc = q.main()
    if run_dir_text:
        annotate_status(
            Path(run_dir_text),
            stats,
            max_leg_age_ms=max_leg_age_ms,
            max_cross_leg_skew_ms=max_cross_leg_skew_ms,
            max_exchange_snapshot_age_ms=max_exchange_snapshot_age_ms,
            max_exchange_snapshot_skew_ms=max_exchange_snapshot_skew_ms,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
