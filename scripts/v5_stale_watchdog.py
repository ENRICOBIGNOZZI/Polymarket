#!/usr/bin/env python3
"""Restart the V5 allocator when a live child stops publishing fresh state.

Process liveness alone is insufficient: a child can be alive while blocked in a
network call or deadlocked. This watchdog reads the allocator's own strategy
status, waits through a startup grace period, and terminates only the allocator
process when a non-killed child is stale. The existing shell supervisor then
restarts the allocator and all of its process-group children.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


def _now() -> int:
    return int(time.time())


def _float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_last_csv(path: Path) -> dict[str, str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return {}
    return rows[-1] if rows else {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_event(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ("timestamp", "allocator_pid", "event", "reason", "details")
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


@dataclass
class WatchState:
    allocator_pid: int = 0
    first_seen: int = 0
    restart_requests: int = 0


def evaluate(
    run_root: Path,
    state: WatchState,
    *,
    stale_seconds: float,
    grace_seconds: float,
    now: int | None = None,
) -> dict[str, Any]:
    current = _now() if now is None else int(now)
    supervisor = _read_last_csv(run_root / "runtime_supervisor.csv")
    pid = _int(supervisor.get("allocator_pid"))
    allocator_alive = _int(supervisor.get("allocator_alive")) == 1 and pid > 0

    if pid != state.allocator_pid:
        state.allocator_pid = pid
        state.first_seen = current

    result: dict[str, Any] = {
        "schema": "polymarket_v5_stale_watchdog_v1",
        "timestamp": current,
        "allocator_pid": pid,
        "allocator_alive": allocator_alive,
        "first_seen": state.first_seen,
        "grace_seconds": grace_seconds,
        "stale_seconds": stale_seconds,
        "restart_requests": state.restart_requests,
        "state": "WAITING_FOR_ALLOCATOR",
        "restart_required": False,
        "reason": "",
        "stale_models": [],
    }
    if not allocator_alive:
        return result

    allocator = _read_json(run_root / "allocator_status.json")
    if bool(allocator.get("killed", False)):
        result["state"] = "GLOBAL_KILL_ACTIVE"
        return result

    age_since_seen = max(0, current - state.first_seen)
    if age_since_seen < grace_seconds:
        result["state"] = "STARTUP_GRACE"
        return result

    status_path = run_root / "strategy_status.csv"
    rows = _read_csv(status_path)
    try:
        file_age = max(0.0, current - status_path.stat().st_mtime)
    except OSError:
        file_age = 1e12

    if not rows:
        result.update(
            state="STALE",
            restart_required=True,
            reason="strategy_status_missing",
            status_file_age_seconds=file_age,
        )
        return result

    stale_models: list[str] = []
    for row in rows:
        if _int(row.get("killed")) == 1:
            continue
        age = _float(row.get("status_age_seconds"), 1e12)
        if age > stale_seconds:
            stale_models.append(str(row.get("name", "unknown")))

    result["status_file_age_seconds"] = file_age
    result["stale_models"] = sorted(set(stale_models))
    if file_age > stale_seconds:
        result.update(state="STALE", restart_required=True, reason="strategy_status_file_stale")
    elif stale_models:
        result.update(state="STALE", restart_required=True, reason="child_status_stale")
    else:
        result["state"] = "HEALTHY"
    return result


def request_restart(run_root: Path, state: WatchState, decision: dict[str, Any]) -> bool:
    pid = _int(decision.get("allocator_pid"))
    if not bool(decision.get("restart_required")) or pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        _append_event(
            run_root / "stale_watchdog_events.csv",
            {
                "timestamp": _now(),
                "allocator_pid": pid,
                "event": "restart_failed",
                "reason": decision.get("reason", ""),
                "details": str(exc),
            },
        )
        return False

    state.restart_requests += 1
    _append_event(
        run_root / "stale_watchdog_events.csv",
        {
            "timestamp": _now(),
            "allocator_pid": pid,
            "event": "restart_requested",
            "reason": decision.get("reason", ""),
            "details": "|".join(str(x) for x in decision.get("stale_models", [])),
        },
    )
    state.allocator_pid = 0
    state.first_seen = _now()
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--stale-seconds", type=float, default=600.0)
    parser.add_argument("--grace-seconds", type=float, default=180.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.stale_seconds <= 0 or args.grace_seconds < 0 or args.interval_seconds <= 0:
        raise SystemExit("invalid watchdog timing")
    state = WatchState()
    while True:
        try:
            decision = evaluate(
                args.run_root,
                state,
                stale_seconds=args.stale_seconds,
                grace_seconds=args.grace_seconds,
            )
            if bool(decision.get("restart_required")) and not args.dry_run:
                request_restart(args.run_root, state, decision)
                decision["restart_requests"] = state.restart_requests
            _atomic_json(args.run_root / "stale_watchdog_status.json", decision)
        except Exception as exc:  # keep the safety monitor alive and visible
            _atomic_json(
                args.run_root / "stale_watchdog_status.json",
                {
                    "schema": "polymarket_v5_stale_watchdog_v1",
                    "timestamp": _now(),
                    "state": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
