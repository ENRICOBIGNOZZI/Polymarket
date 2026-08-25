#!/usr/bin/env python3
"""Restart stale V5 components even when their processes are still alive.

The allocator has its own child-status surface. The maker and periodic scanners
run in the champion shell, so their file publication cadence is the liveness
surface. The watchdog waits through separate startup grace periods, restarts
only the allocator for stale generic children, and terminates the champion shell
for a frozen executable backend. The outer live supervisor then restarts the
paper champion from persisted state.
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
from typing import Any, Iterable, Sequence


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


def _file_age(path: Path, current: int) -> float:
    try:
        return max(0.0, current - path.stat().st_mtime)
    except OSError:
        return 1e12


def _newest_age(paths: Iterable[Path], current: int) -> float:
    return min((_file_age(path, current) for path in paths), default=1e12)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_event(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ("timestamp", "target", "target_pid", "event", "reason", "details")
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


@dataclass
class WatchState:
    allocator_pid: int = 0
    allocator_first_seen: int = 0
    main_pid: int = 0
    main_first_seen: int = 0
    restart_requests: int = 0


def _backend_ages(run_root: Path, current: int) -> dict[str, float]:
    groups = {
        "runtime_supervisor": [run_root / "runtime_supervisor.csv"],
        "maker": [run_root / "maker" / "maker_equity.csv", run_root / "maker.log"],
        "pair_scanner": [
            run_root / "stat_arb_pairs.csv",
            run_root / "stat_arb_pairs_latest.log",
            run_root / "stat_arb_pairs_errors.log",
        ],
        "pca_scanner": [
            run_root / "stat_arb_pca.csv",
            run_root / "stat_arb_pca_raw.csv",
            run_root / "stat_arb_pca_latest.log",
            run_root / "stat_arb_pca_errors.log",
        ],
        "structural_scanner": [
            run_root / "structural_latest.csv",
            run_root / "structural_latest.log",
            run_root / "structural_errors.log",
        ],
        "action_report": [
            run_root / "action_report.json",
            run_root / "action_report_latest.log",
            run_root / "action_report_errors.log",
        ],
    }
    return {name: _newest_age(paths, current) for name, paths in groups.items()}


def evaluate(
    run_root: Path,
    state: WatchState,
    *,
    stale_seconds: float,
    grace_seconds: float,
    main_pid: int = 0,
    backend_stale_seconds: float = 900.0,
    backend_grace_seconds: float = 900.0,
    now: int | None = None,
) -> dict[str, Any]:
    current = _now() if now is None else int(now)
    supervisor = _read_last_csv(run_root / "runtime_supervisor.csv")
    allocator_pid = _int(supervisor.get("allocator_pid"))
    allocator_alive = _int(supervisor.get("allocator_alive")) == 1 and allocator_pid > 0

    if allocator_pid != state.allocator_pid:
        state.allocator_pid = allocator_pid
        state.allocator_first_seen = current
    if main_pid != state.main_pid:
        state.main_pid = max(0, int(main_pid))
        state.main_first_seen = current

    allocator_age = max(0, current - state.allocator_first_seen)
    main_age = max(0, current - state.main_first_seen)
    allocator = _read_json(run_root / "allocator_status.json")
    global_kill = bool(allocator.get("killed", False))

    result: dict[str, Any] = {
        "schema": "polymarket_v5_stale_watchdog_v2",
        "timestamp": current,
        "allocator_pid": allocator_pid,
        "allocator_alive": allocator_alive,
        "allocator_first_seen": state.allocator_first_seen,
        "main_pid": state.main_pid,
        "main_first_seen": state.main_first_seen,
        "grace_seconds": grace_seconds,
        "stale_seconds": stale_seconds,
        "backend_grace_seconds": backend_grace_seconds,
        "backend_stale_seconds": backend_stale_seconds,
        "restart_requests": state.restart_requests,
        "state": "WAITING_FOR_ALLOCATOR",
        "restart_required": False,
        "restart_target": "",
        "restart_target_pid": 0,
        "reason": "",
        "stale_models": [],
        "stale_backends": [],
        "backend_age_seconds": {},
        "global_kill": global_kill,
    }

    allocator_issue = ""
    status_path = run_root / "strategy_status.csv"
    status_file_age = _file_age(status_path, current)
    result["status_file_age_seconds"] = status_file_age
    if allocator_alive and not global_kill and allocator_age >= grace_seconds:
        rows = _read_csv(status_path)
        if not rows:
            allocator_issue = "strategy_status_missing"
        elif status_file_age > stale_seconds:
            allocator_issue = "strategy_status_file_stale"
        else:
            stale_models = sorted(
                {
                    str(row.get("name", "unknown"))
                    for row in rows
                    if _int(row.get("killed")) != 1
                    and _float(row.get("status_age_seconds"), 1e12) > stale_seconds
                }
            )
            result["stale_models"] = stale_models
            if stale_models:
                allocator_issue = "child_status_stale"

    backend_issue = ""
    if state.main_pid > 0 and main_age >= backend_grace_seconds:
        ages = _backend_ages(run_root, current)
        stale_backends = sorted(name for name, age in ages.items() if age > backend_stale_seconds)
        result["backend_age_seconds"] = ages
        result["stale_backends"] = stale_backends
        if stale_backends:
            backend_issue = "backend_output_stale"

    # A frozen champion shell is the broader failure and restarts every persisted
    # component. Otherwise restart only the generic allocator process.
    if backend_issue:
        result.update(
            state="STALE",
            restart_required=True,
            restart_target="main",
            restart_target_pid=state.main_pid,
            reason=backend_issue,
        )
    elif allocator_issue:
        result.update(
            state="STALE",
            restart_required=True,
            restart_target="allocator",
            restart_target_pid=allocator_pid,
            reason=allocator_issue,
        )
    elif not allocator_alive:
        result["state"] = "WAITING_FOR_ALLOCATOR"
    elif global_kill:
        result["state"] = "GLOBAL_KILL_ACTIVE"
    elif allocator_age < grace_seconds or (state.main_pid > 0 and main_age < backend_grace_seconds):
        result["state"] = "STARTUP_GRACE"
    else:
        result["state"] = "HEALTHY"
    return result


def request_restart(run_root: Path, state: WatchState, decision: dict[str, Any]) -> bool:
    pid = _int(decision.get("restart_target_pid"))
    target = str(decision.get("restart_target", ""))
    if not bool(decision.get("restart_required")) or pid <= 0 or target not in {"allocator", "main"}:
        return False

    details = "|".join(
        str(value)
        for value in (decision.get("stale_models", []) or decision.get("stale_backends", []))
    )
    _append_event(
        run_root / "stale_watchdog_events.csv",
        {
            "timestamp": _now(),
            "target": target,
            "target_pid": pid,
            "event": "restart_requested",
            "reason": decision.get("reason", ""),
            "details": details,
        },
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        _append_event(
            run_root / "stale_watchdog_events.csv",
            {
                "timestamp": _now(),
                "target": target,
                "target_pid": pid,
                "event": "restart_failed",
                "reason": decision.get("reason", ""),
                "details": str(exc),
            },
        )
        return False

    state.restart_requests += 1
    if target == "allocator":
        state.allocator_pid = 0
        state.allocator_first_seen = _now()
    else:
        state.main_pid = 0
        state.main_first_seen = _now()
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--main-pid", type=int, default=0)
    parser.add_argument("--stale-seconds", type=float, default=600.0)
    parser.add_argument("--grace-seconds", type=float, default=180.0)
    parser.add_argument("--backend-stale-seconds", type=float, default=900.0)
    parser.add_argument("--backend-grace-seconds", type=float, default=900.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        args.main_pid < 0
        or args.stale_seconds <= 0
        or args.grace_seconds < 0
        or args.backend_stale_seconds <= 0
        or args.backend_grace_seconds < 0
        or args.interval_seconds <= 0
    ):
        raise SystemExit("invalid watchdog timing or pid")
    state = WatchState()
    while True:
        try:
            decision = evaluate(
                args.run_root,
                state,
                stale_seconds=args.stale_seconds,
                grace_seconds=args.grace_seconds,
                main_pid=args.main_pid,
                backend_stale_seconds=args.backend_stale_seconds,
                backend_grace_seconds=args.backend_grace_seconds,
            )
            if bool(decision.get("restart_required")) and not args.dry_run:
                request_restart(args.run_root, state, decision)
                decision["restart_requests"] = state.restart_requests
            _atomic_json(args.run_root / "stale_watchdog_status.json", decision)
        except Exception as exc:  # keep the safety monitor alive and visible
            _atomic_json(
                args.run_root / "stale_watchdog_status.json",
                {
                    "schema": "polymarket_v5_stale_watchdog_v2",
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
