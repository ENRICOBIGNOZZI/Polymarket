#!/usr/bin/env python3
from __future__ import annotations

# Frozen depth-aware/strict-markout adapter. The canonical v7_micro_maker_worker
# wraps this module with explicit CANCEL_PENDING semantics. This temporary split
# is removed in the post-V7 cleanup when the canonical maker is flattened.

import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import v7_micro_maker_worker_eventtime_core as core

MAX_MARKOUT_LABEL_DELAY_SECONDS = 15
_DEPTH_EXIT_SHARES: dict[str, float] = {}
_ORIGINAL_BOOK_BID = core.base.Book.bid


def _run_dir(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--run-dir")
        return Path(argv[index + 1])
    except (ValueError, IndexError):
        return None


def _finite(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out


def full_depth_sell_vwap(levels: list[tuple[float, float]], shares: float) -> float | None:
    target = max(0.0, float(shares))
    if target <= 1e-12:
        return None
    remaining = target
    proceeds = 0.0
    for price, size in sorted(levels, key=lambda item: item[0], reverse=True):
        px = _finite(price)
        qty = max(0.0, _finite(size, 0.0))
        if not math.isfinite(px) or px <= 0.0 or qty <= 0.0:
            continue
        take = min(remaining, qty)
        proceeds += take * px
        remaining -= take
        if remaining <= 1e-12:
            return proceeds / target
    return None


def _exit_requirements(run_dir: Path) -> dict[str, float]:
    path = run_dir / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict):
        return {}
    position_shares: dict[str, float] = {}
    order_shares: dict[str, float] = {}
    watch_shares: dict[str, float] = {}
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    watches = state.get("markout_watch") if isinstance(state.get("markout_watch"), dict) else {}
    for row in positions.values():
        if isinstance(row, dict):
            token = str(row.get("token_id") or "")
            if token:
                position_shares[token] = position_shares.get(token, 0.0) + max(0.0, _finite(row.get("shares"), 0.0))
    for row in orders.values():
        if isinstance(row, dict):
            token = str(row.get("token_id") or "")
            if token:
                order_shares[token] = order_shares.get(token, 0.0) + max(0.0, _finite(row.get("remaining_shares"), 0.0))
    for row in watches.values():
        if isinstance(row, dict):
            token = str(row.get("token_id") or "")
            if token:
                watch_shares[token] = max(watch_shares.get(token, 0.0), max(0.0, _finite(row.get("shares"), 0.0)))
    killed = bool(state.get("killed"))
    required: dict[str, float] = {}
    for token in set(position_shares) | set(order_shares) | set(watch_shares):
        live_position = position_shares.get(token, 0.0)
        possible_exit = live_position
        if live_position > 0.0 or killed:
            possible_exit += order_shares.get(token, 0.0)
        required[token] = max(possible_exit, watch_shares.get(token, 0.0))
    return {token: shares for token, shares in required.items() if shares > 1e-12}


def _depth_aware_bid(book: Any) -> float:
    shares = max(0.0, _DEPTH_EXIT_SHARES.get(str(getattr(book, "token", "")), 0.0))
    if shares <= 1e-12:
        return _ORIGINAL_BOOK_BID.fget(book)  # type: ignore[union-attr]
    value = full_depth_sell_vwap(list(getattr(book, "bids", [])), shares)
    return value if value is not None else math.nan


def _atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in rows])
    os.replace(tmp, path)


def filter_late_markouts(run_dir: Path, max_label_delay_seconds: int = MAX_MARKOUT_LABEL_DELAY_SECONDS) -> dict[str, int]:
    path = run_dir / "maker_markouts.csv"
    if not path.exists():
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": 0}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except OSError:
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": 0}
    if not fields:
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": len(rows)}
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    late = invalid = 0
    bound = max(0, int(max_label_delay_seconds))
    for row in rows:
        horizon = _finite(row.get("horizon_seconds"))
        age = _finite(row.get("observed_age_seconds"))
        if horizon <= 0 or age < horizon or not (math.isfinite(horizon) and math.isfinite(age)):
            invalid += 1
            rejected.append({**row, "label_delay_seconds": "", "reject_reason": "invalid_markout_clock"})
            continue
        delay = age - horizon
        if delay > bound + 1e-12:
            late += 1
            rejected.append({**row, "label_delay_seconds": f"{delay:.12g}", "reject_reason": "late_markout_label"})
            continue
        kept.append(row)
    _atomic_csv(path, fields, kept)
    if rejected:
        reject_path = run_dir / "maker_markout_rejections.csv"
        reject_fields = fields + ["label_delay_seconds", "reject_reason"]
        existing: list[dict[str, Any]] = []
        if reject_path.exists():
            try:
                with reject_path.open(newline="", encoding="utf-8") as handle:
                    existing = [dict(row) for row in csv.DictReader(handle)]
            except OSError:
                existing = []
        _atomic_csv(reject_path, reject_fields, existing + rejected)
    return {"kept": len(kept), "rejected_late": late, "rejected_invalid": invalid}


def _annotate(run_dir: Path, stats: dict[str, int], requirements: dict[str, float]) -> None:
    for name in ("status.json", "state.json"):
        path = run_dir / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        value["markout_label_contract"] = "event_time_horizon_with_bounded_observation_delay"
        value["markout_max_label_delay_seconds"] = MAX_MARKOUT_LABEL_DELAY_SECONDS
        value["markout_rows_kept"] = int(stats.get("kept", 0))
        value["markout_rows_rejected_late"] = int(stats.get("rejected_late", 0))
        value["markout_rows_rejected_invalid"] = int(stats.get("rejected_invalid", 0))
        value["exit_liquidity_contract"] = "shares_specific_full_visible_bid_depth_vwap_fail_closed"
        value["depth_aware_exit_tokens"] = len(requirements)
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


for _name in dir(core):
    if not _name.startswith("__") and _name != "main":
        globals()[_name] = getattr(core, _name)
globals()["full_depth_sell_vwap"] = full_depth_sell_vwap
globals()["filter_late_markouts"] = filter_late_markouts


def main() -> int:
    run_dir = _run_dir(list(sys.argv))
    requirements = _exit_requirements(run_dir) if run_dir is not None else {}
    _DEPTH_EXIT_SHARES.clear()
    _DEPTH_EXIT_SHARES.update(requirements)
    original_bid = core.base.Book.bid
    core.base.Book.bid = property(_depth_aware_bid)
    try:
        rc = core.main()
    finally:
        core.base.Book.bid = original_bid
        _DEPTH_EXIT_SHARES.clear()
    if run_dir is not None:
        stats = filter_late_markouts(run_dir)
        _annotate(run_dir, stats, requirements)
    return rc
