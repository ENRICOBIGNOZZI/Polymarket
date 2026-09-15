#!/usr/bin/env python3
"""Serialize retrospective V7 analytics so they cannot contend with the hot path."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--max-load-per-cpu", type=float, default=1.25)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    cpu_count = max(1, int(os.cpu_count() or 1))
    load1 = float(os.getloadavg()[0])
    max_load = max(0.1, float(args.max_load_per_cpu)) * cpu_count
    if load1 > max_load:
        if args.status:
            atomic_json(args.status, {
                "schema": "polymarket_v7_serialized_analytics_status_v1",
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "real_capital_at_risk": False,
                "state": "DEFERRED_RESOURCE_PRESSURE", "command": command,
                "load1": load1, "cpu_count": cpu_count,
                "max_load_per_cpu": float(args.max_load_per_cpu),
                "timestamp_ns": time.time_ns(),
            })
        return 75
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    started_ns = time.time_ns()
    with args.lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired_ns = time.time_ns()
        if args.status:
            atomic_json(args.status, {
                "schema": "polymarket_v7_serialized_analytics_status_v1",
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "real_capital_at_risk": False,
                "state": "RUNNING",
                "command": command,
                "wait_ns": max(0, acquired_ns - started_ns),
                "started_ns": acquired_ns,
            })
        result = subprocess.run(command, check=False)
        finished_ns = time.time_ns()
        if args.status:
            atomic_json(args.status, {
                "schema": "polymarket_v7_serialized_analytics_status_v1",
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "real_capital_at_risk": False,
                "state": "IDLE",
                "command": command,
                "wait_ns": max(0, acquired_ns - started_ns),
                "duration_ns": max(0, finished_ns - acquired_ns),
                "returncode": int(result.returncode),
                "finished_ns": finished_ns,
            })
        return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
